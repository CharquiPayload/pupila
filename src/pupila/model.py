"""What Pupila knows about the server: rooms, spaces, members and messages.

Fed from /sync and /messages responses; it never talks to the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MESSAGE_TYPES = {"m.room.message", "m.sticker"}
MAX_EVENTS = 400  # per room; older ones are dropped (they can be fetched again)


@dataclass
class Event:
    event_id: str
    sender: str
    ts: int
    type: str
    content: dict
    redacted: bool = False
    edited: bool = False
    reactions: dict[str, dict[str, str]] = field(default_factory=dict)  # key -> {sender: reaction id}
    pending: bool = False  # local echo, not confirmed yet
    failed: bool = False

    @property
    def msgtype(self) -> str:
        return self.content.get("msgtype", "m.sticker" if self.type == "m.sticker" else "")

    @property
    def reply_to(self) -> str | None:
        return (self.content.get("m.relates_to") or {}).get("m.in_reply_to", {}).get("event_id")

    @property
    def thread(self) -> str | None:
        rel = self.content.get("m.relates_to") or {}
        return rel.get("event_id") if rel.get("rel_type") == "m.thread" else None

    @property
    def text(self) -> str:
        """The body without the reply fallback quote ('> <@x> ...')."""
        body = self.content.get("body", "")
        if self.reply_to and body.startswith("> "):
            lines = body.split("\n")
            i = 0
            while i < len(lines) and lines[i].startswith(">"):
                i += 1
            body = "\n".join(lines[i:]).lstrip("\n")
        return body


@dataclass
class Room:
    room_id: str
    name: str = ""
    topic: str = ""
    alias: str = ""
    is_space: bool = False
    children: dict[str, dict] = field(default_factory=dict)  # room_id -> m.space.child content
    members: dict[str, str] = field(default_factory=dict)  # user_id -> displayname
    heroes: list[str] = field(default_factory=list)
    member_count: int = 0
    events: list[Event] = field(default_factory=list)
    index: dict[str, Event] = field(default_factory=dict)
    prev_batch: str | None = None
    unread: int = 0
    highlights: int = 0
    typing: set[str] = field(default_factory=set)
    invited: bool = False
    invited_by: str = ""
    last_ts: int = 0
    dm_with: str | None = None

    def add(self, ev: Event, at_end: bool = True) -> bool:
        if ev.event_id in self.index:
            return False
        self.index[ev.event_id] = ev
        if at_end:
            self.events.append(ev)
        else:
            self.events.insert(0, ev)
        if ev.type in MESSAGE_TYPES:
            self.last_ts = max(self.last_ts, ev.ts)
        if len(self.events) > MAX_EVENTS:
            old = self.events.pop(0)
            self.index.pop(old.event_id, None)
        return True


@dataclass
class Changes:
    rooms: set[str] = field(default_factory=set)       # rooms with something new
    structure: bool = False                             # rooms or spaces appearing/disappearing
    new: list[tuple[str, Event]] = field(default_factory=list)  # new messages (for notifications)
    updated: set[str] = field(default_factory=set)     # edited/redacted/reacted event ids
    limited: set[str] = field(default_factory=set)     # rooms whose history has a gap


class Store:
    def __init__(self, me: str):
        self.me = me
        self.rooms: dict[str, Room] = {}
        self.directs: dict[str, str] = {}  # room_id -> user_id
        self.names: dict[str, str] = {}    # user_id -> displayname seen in any room
        self.reactions: dict[str, tuple[str, str, str]] = {}  # reaction id -> (event, key, sender)

    # --- names ---

    def user_name(self, user_id: str, room: Room | None = None) -> str:
        if room and room.members.get(user_id):
            return room.members[user_id]
        if self.names.get(user_id):
            return self.names[user_id]
        return user_id.split(":")[0].lstrip("@")

    def room_name(self, room: Room) -> str:
        if room.name:
            return room.name
        if room.alias:
            return room.alias
        other = room.dm_with or next((h for h in room.heroes if h != self.me), None)
        if other:
            return self.user_name(other, room)
        others = [u for u in room.members if u != self.me]
        if others:
            return ", ".join(self.user_name(u, room) for u in others[:3])
        return "Empty room"

    # --- sync ---

    def apply_sync(self, d: dict, initial: bool = False) -> Changes:
        c = Changes()
        for ev in (d.get("account_data") or {}).get("events", []):
            if ev.get("type") == "m.direct":
                self.directs = {r: u for u, rooms in (ev.get("content") or {}).items() for r in rooms}
                for rid, r in self.rooms.items():
                    r.dm_with = self.directs.get(rid)
                c.structure = True
        rooms = d.get("rooms") or {}
        for rid, r in (rooms.get("join") or {}).items():
            is_new = rid not in self.rooms or self.rooms[rid].invited
            room = self.rooms.get(rid) or Room(rid)
            room.invited = False
            self.rooms[rid] = room
            room.dm_with = self.directs.get(rid)
            if is_new:
                c.structure = True
            summary = r.get("summary") or {}
            if "m.heroes" in summary:
                room.heroes = summary["m.heroes"]
            if "m.joined_member_count" in summary:
                room.member_count = summary["m.joined_member_count"]
            for ev in (r.get("state") or {}).get("events", []):
                if self._state(room, ev):
                    c.structure = True
            tl = r.get("timeline") or {}
            if tl.get("limited") and not initial:
                room.events.clear()
                room.index.clear()
                c.limited.add(rid)
            if tl.get("prev_batch") and (not room.prev_batch or tl.get("limited") or initial):
                room.prev_batch = tl["prev_batch"]
            for ev in tl.get("events", []):
                if "state_key" in ev and self._state(room, ev):
                    c.structure = True
                new = self._event(room, ev, c)
                if new and not initial and new.sender != self.me and new.type in MESSAGE_TYPES:
                    c.new.append((rid, new))
            for ev in (r.get("ephemeral") or {}).get("events", []):
                if ev.get("type") == "m.typing":
                    room.typing = set(ev.get("content", {}).get("user_ids", [])) - {self.me}
                    c.rooms.add(rid)
            counts = r.get("unread_notifications") or {}
            if counts:
                room.unread = counts.get("notification_count", 0) or 0
                room.highlights = counts.get("highlight_count", 0) or 0
            c.rooms.add(rid)
        for rid, r in (rooms.get("invite") or {}).items():
            room = self.rooms.get(rid) or Room(rid, invited=True)
            room.invited = True
            self.rooms[rid] = room
            for ev in (r.get("invite_state") or {}).get("events", []):
                self._state(room, ev)
                if ev.get("type") == "m.room.member" and ev.get("state_key") == self.me:
                    room.invited_by = ev.get("sender", "")
            c.structure = True
        for rid in (rooms.get("leave") or {}):
            if self.rooms.pop(rid, None):
                c.structure = True
        return c

    def apply_history(self, rid: str, d: dict) -> Changes:
        """A /messages response (dir=b): events from newest to oldest."""
        c = Changes()
        room = self.rooms[rid]
        for ev in d.get("state") or []:
            self._state(room, ev)
        for ev in d.get("chunk") or []:
            if "state_key" in ev:
                self._state(room, ev)
            self._event(room, ev, c, at_end=False)
        room.prev_batch = d.get("end") if d.get("chunk") else None
        c.rooms.add(rid)
        return c

    # --- internals ---

    def _state(self, room: Room, ev: dict) -> bool:
        """Applies a state event. Returns True if the structure changes (spaces, names)."""
        t, k, content = ev.get("type"), ev.get("state_key"), ev.get("content") or {}
        if t == "m.room.name":
            changed = room.name != content.get("name", "")
            room.name = content.get("name", "")
            return changed
        if t == "m.room.topic":
            room.topic = content.get("topic", "")
        elif t == "m.room.canonical_alias":
            room.alias = content.get("alias", "") or ""
        elif t == "m.room.create":
            if content.get("type") == "m.space" and not room.is_space:
                room.is_space = True
                return True
        elif t == "m.space.child" and k:
            if content.get("via"):
                if k not in room.children:
                    room.children[k] = content
                    return True
                room.children[k] = content
            elif room.children.pop(k, None) is not None:
                return True
        elif t == "m.room.member" and k:
            if content.get("membership") in ("join", "invite"):
                name = content.get("displayname") or ""
                room.members[k] = name
                if name:
                    self.names[k] = name
            else:
                room.members.pop(k, None)
        return False

    def _event(self, room: Room, ev: dict, c: Changes, at_end: bool = True) -> Event | None:
        t = ev.get("type", "")
        content = ev.get("content") or {}
        rel = content.get("m.relates_to") or {}
        eid = ev.get("event_id", "")
        if t == "m.room.redaction":
            self._redact(room, content.get("redacts") or ev.get("redacts"), c)
            return None
        if t == "m.reaction" and rel.get("rel_type") == "m.annotation":
            target = room.index.get(rel.get("event_id", ""))
            key = rel.get("key", "")
            self.reactions[eid] = (rel.get("event_id", ""), key, ev.get("sender", ""))
            if target and not (ev.get("unsigned") or {}).get("redacted_because"):
                target.reactions.setdefault(key, {})[ev.get("sender", "")] = eid
                c.updated.add(target.event_id)
                c.rooms.add(room.room_id)
            return None
        if rel.get("rel_type") == "m.replace" and "m.new_content" in content:
            target = room.index.get(rel.get("event_id", ""))
            if target and target.sender == ev.get("sender"):
                target.content = {**content["m.new_content"],
                                  "m.relates_to": target.content.get("m.relates_to", {})}
                target.edited = True
                c.updated.add(target.event_id)
                c.rooms.add(room.room_id)
            return None
        if "state_key" in ev or t not in MESSAGE_TYPES:
            # names, topics, permissions, joins and leaves: not shown in the chat
            return None
        echo = self._confirm_echo(room, ev)
        if echo:
            c.updated.add(echo.event_id)
            return None
        new = Event(eid, ev.get("sender", ""), ev.get("origin_server_ts", 0), t, content,
                    redacted=bool((ev.get("unsigned") or {}).get("redacted_because")) or not content)
        if room.add(new, at_end):
            c.rooms.add(room.room_id)
            return new
        return None

    def _confirm_echo(self, room: Room, ev: dict) -> Event | None:
        """If it's our own message that was shown as a local echo ("~txn"), confirms it."""
        if ev.get("sender") != self.me:
            return None
        eid = ev.get("event_id", "")
        txn = (ev.get("unsigned") or {}).get("transaction_id")
        e = room.index.pop("~" + txn, None) if txn else None
        if e:
            e.event_id = eid
            room.index[eid] = e
        else:
            e = room.index.get(eid)
            if not (e and e.pending):
                return None
        e.pending = False
        e.ts = ev.get("origin_server_ts", e.ts)
        return e

    def _redact(self, room: Room, target_id: str | None, c: Changes) -> None:
        if not target_id:
            return
        if target_id in self.reactions:
            event_id, key, sender = self.reactions.pop(target_id)
            target = room.index.get(event_id)
            if target and key in target.reactions:
                target.reactions[key].pop(sender, None)
                if not target.reactions[key]:
                    del target.reactions[key]
                c.updated.add(event_id)
                c.rooms.add(room.room_id)
            return
        e = room.index.get(target_id)
        if e:
            e.redacted = True
            e.content = {}
            e.reactions.clear()
            c.updated.add(target_id)
            c.rooms.add(room.room_id)

    # --- local echo ---

    def echo(self, rid: str, event_id: str, content: dict, ts: int) -> Event:
        e = Event(event_id, self.me, ts, "m.room.message", content, pending=True)
        self.rooms[rid].add(e)
        return e

    def rename(self, rid: str, old: str, new: str) -> Event | None:
        """The local echo gets its real event_id once the send finishes."""
        room = self.rooms[rid]
        e = room.index.pop(old, None)
        if not e:  # the sync already confirmed it and gave it its id
            return room.index.get(new)
        if new in room.index:  # the sync brought it first, without recognising it as an echo
            room.events.remove(e)
            real = room.index[new]
            real.pending = False
            return real
        e.event_id = new
        e.pending = False
        room.index[new] = e
        return e

    # --- space tree ---

    def roots(self) -> list[Room]:
        """Top-level spaces (not children of another space I'm in)."""
        children = {h for r in self.rooms.values() if r.is_space for h in r.children}
        return [r for r in self.rooms.values() if r.is_space and r.room_id not in children
                and not r.invited]

    def orphans(self) -> list[Room]:
        """Rooms that aren't in any space."""
        children = {h for r in self.rooms.values() if r.is_space for h in r.children}
        return [r for r in self.rooms.values() if not r.is_space and r.room_id not in children
                and not r.invited]

    def mentions_me(self, e: Event) -> bool:
        local = self.me.split(":")[0].lstrip("@")
        name = self.names.get(self.me, "")
        body = e.content.get("body", "")
        if self.me in body or self.me in e.content.get("formatted_body", ""):
            return True
        return any(n and re.search(rf"\b{re.escape(n)}\b", body, re.I) for n in (local, name))
