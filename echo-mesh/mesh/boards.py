"""Message boards: text-only discussion boards between computers/AIs (for example Echo <-> a partner AI) that BOTH owners can read.

Rules, all enforced here and not by a prompt:
  * text only, short, control characters removed; a post is data and is never run or fed to a tool by the Mesh
  * a computer sees only the boards it is a member of; the owner of the Mesh (admin) sees every board and every post, held ones too
  * what leaves the owner's side to someone else's computer goes through the outbound filter (secrets, personal details, school work);
    a post with a private key or school work is refused, not cleaned
  * the owner's approval switch (on by default): posts written on the owner's side wait for a yes before the other side can read them
  * a rate limit per author so an AI cannot flood a board
Written once; the coordinator serves it to the Mac plugin and to the .exe through the same node API.
"""
import re, time, uuid

from . import policy

MAX_POST = 2000
MAX_TITLE = 80
RATE_N, RATE_S = 20, 600
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s, limit):
    return CTRL.sub("", str(s or "")).strip()[:limit]


class Boards:
    def __init__(self, store):
        self.s = store

    # ---- owner side (admin API) ----
    def create(self, title, members, approval=True):
        title = clean_text(title, MAX_TITLE)
        if not title:
            raise ValueError("a board needs a title")
        bid = "b-" + uuid.uuid4().hex[:8]
        self.s.x("INSERT INTO boards(id,title,approval,status,created) VALUES(?,?,?,?,?)", (bid, title, int(bool(approval)), "open", time.time()))
        for m in members:
            self.add_member(bid, m)
        self.s.audit("board-created", board=bid, title=title)
        return bid

    def add_member(self, bid, node_id):
        if not self.s.one("SELECT 1 FROM nodes WHERE id=? AND status='approved'", (node_id,)):
            raise ValueError("only an approved computer can join a board")
        self.s.x("INSERT OR IGNORE INTO board_members(board,node) VALUES(?,?)", (bid, node_id))

    def set_approval(self, bid, on):
        return self.s.x("UPDATE boards SET approval=? WHERE id=?", (int(bool(on)), bid)) > 0

    def close(self, bid):
        return self.s.x("UPDATE boards SET status='closed' WHERE id=?", (bid,)) > 0

    def _outside_members(self, bid):
        return self.s.q("SELECT n.id FROM board_members m JOIN nodes n ON n.id=m.node WHERE m.board=? AND n.lan=0", (bid,))

    def owner_post(self, bid, author, body):
        """Written on the owner's side (the owner or Echo). -> (post_id, status, reason)."""
        b = self.s.one("SELECT * FROM boards WHERE id=? AND status='open'", (bid,))
        if not b:
            return None, "refused", "no such open board"
        text = clean_text(body, MAX_POST)
        if not text:
            return None, "refused", "empty post"
        if not self._rate_ok(bid, "owner:" + author):
            return None, "refused", "too many posts too fast"
        found = []
        if self._outside_members(bid):                     # someone else's computer will read it
            ok, text, why, found = policy.check_dispatch("shared", text, True)
            if not ok:
                self.s.audit("post-refused", board=bid, reason=why)
                return None, "refused", why
        status = "held" if b["approval"] and self._outside_members(bid) else "posted"
        pid = self._insert(bid, "owner:" + author, author, text, status)
        return pid, status, ("removed: " + ", ".join(found)) if found else ""

    def decide(self, pid, approve):
        st = "posted" if approve else "rejected"
        n = self.s.x("UPDATE posts SET status=?, decided=? WHERE id=? AND status='held'", (st, time.time(), pid))
        self.s.audit("post-" + ("approved" if approve else "rejected"), post=pid)
        return n > 0

    def held(self):
        return self.s.q("SELECT p.id, p.board, b.title, p.author_name, p.body, p.created FROM posts p JOIN boards b ON b.id=p.board WHERE p.status='held' ORDER BY p.created")

    def all_boards(self):
        out = []
        for b in self.s.q("SELECT * FROM boards ORDER BY created DESC"):
            b["members"] = [r["name"] for r in self.s.q("SELECT n.name FROM board_members m JOIN nodes n ON n.id=m.node WHERE m.board=?", (b["id"],))]
            b["posts"] = self.s.one("SELECT COUNT(*) c FROM posts WHERE board=? AND status='posted'", (b["id"],))["c"]
            b["held"] = self.s.one("SELECT COUNT(*) c FROM posts WHERE board=? AND status='held'", (b["id"],))["c"]
            out.append(b)
        return out

    def read_all(self, bid, since=0):
        return self.s.q("SELECT id, author_name, author, body, status, created FROM posts WHERE board=? AND created>? ORDER BY created", (bid, since))

    # ---- a computer's side (node API) ----
    def boards_for(self, node):
        return [{"id": b["id"], "title": b["title"], "status": b["status"]} for b in
                self.s.q("SELECT b.* FROM boards b JOIN board_members m ON m.board=b.id WHERE m.node=? ORDER BY b.created DESC", (node["id"],))]

    def _member(self, bid, node):
        return bool(self.s.one("SELECT 1 FROM board_members WHERE board=? AND node=?", (bid, node["id"])))

    def read(self, node, bid, since=0):
        if not self._member(bid, node):
            return None
        rows = self.s.q("SELECT id, author, author_name, body, created FROM posts WHERE board=? AND status='posted' AND created>? ORDER BY created", (bid, since))
        return [{"id": r["id"], "author_name": r["author_name"], "body": r["body"], "created": r["created"], "mine": r["author"] == "node:" + node["id"]} for r in rows]

    def node_post(self, node, bid, body):
        """-> (post_id, reason). A computer's own AI posts here; the owner (the owner) reads it as it arrives."""
        b = self.s.one("SELECT * FROM boards WHERE id=? AND status='open'", (bid,))
        if not b or not self._member(bid, node):
            return None, "not a member of an open board with that id"
        text = clean_text(body, MAX_POST)
        if not text:
            return None, "empty post"
        if not self._rate_ok(bid, "node:" + node["id"]):
            return None, "too many posts too fast"
        if node["lan"] and self._outside_members(bid):     # a home computer writing where someone else will read: same outbound filter
            ok, text, why, _ = policy.check_dispatch("shared", text, True)
            if not ok:
                self.s.audit("post-refused", board=bid, reason=why)
                return None, why
        pid = self._insert(bid, "node:" + node["id"], node["name"], text, "posted")
        return pid, ""

    # ---- shared ----
    def _rate_ok(self, bid, author):
        n = self.s.one("SELECT COUNT(*) c FROM posts WHERE board=? AND author=? AND created>?", (bid, author, time.time() - RATE_S))["c"]
        return n < RATE_N

    def _insert(self, bid, author, name, text, status):
        pid = "p-" + uuid.uuid4().hex[:10]
        self.s.x("INSERT INTO posts(id,board,author,author_name,body,status,created) VALUES(?,?,?,?,?,?,?)", (pid, bid, author, clean_text(name, 40), text, status, time.time()))
        self.s.audit("post-" + status, board=bid, post=pid, author=author)
        return pid
