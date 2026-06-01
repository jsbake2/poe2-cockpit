"""PoE2 League Companion — Multi-user Backend."""

import asyncio
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / "data"
STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR.mkdir(exist_ok=True)

import hashlib

SECRETS_PATH = Path(__file__).parent / "data" / "secrets.json"


def _load_users() -> dict:
    """Load user records (with hashed passwords) from data/secrets.json.

    File format: { "users": { "<name>": { "password_hash", ...other fields } } }.
    """
    if not SECRETS_PATH.exists():
        raise RuntimeError(
            f"Missing {SECRETS_PATH}. Run set_password.py to bootstrap users."
        )
    return json.loads(SECRETS_PATH.read_text())["users"]


def verify_password(password: str, encoded: str) -> bool:
    """Verify a password against an encoded scrypt hash.

    Encoded format: scrypt$1$<n>$<r>$<p>$<salt_hex>$<hash_hex>
    """
    try:
        scheme, ver, n, r, p, salt_hex, hash_hex = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    derived = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt_hex),
        n=int(n), r=int(r), p=int(p),
        dklen=len(hash_hex) // 2,
    )
    return secrets.compare_digest(derived.hex(), hash_hex)


USERS = _load_users()

POE_BASE = "https://www.pathofexile.com"
POE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pathofexile.com/",
}

NINJA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}

POLL_INTERVAL = 1800  # 30 minutes
VALID_TOKENS: dict[str, str] = {}  # token -> username


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _user_path(user: str) -> Path:
    return DATA_DIR / f"{user}.json"

SHARED_PATH = DATA_DIR / "shared.json"


def load_user(user: str) -> dict:
    p = _user_path(user)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": {}, "characters": {}}


def save_user(user: str, data: dict):
    _user_path(user).write_text(json.dumps(data, indent=2))


def load_shared() -> dict:
    if SHARED_PATH.exists():
        try:
            return json.loads(SHARED_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"notes": [], "watchlist": [], "builds": []}


def save_shared(data: dict):
    SHARED_PATH.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_user(request: Request) -> str | None:
    token = request.cookies.get("poe2_token")
    return VALID_TOKENS.get(token)


# ---------------------------------------------------------------------------
# PoE character fetching (poe.ninja)
# ---------------------------------------------------------------------------

async def fetch_characters(user: str) -> dict:
    account_full = USERS[user]["poe_account_full"]
    result = {"last_updated": datetime.now(timezone.utc).isoformat(), "list": []}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for league_slug in ["standard", "fatevaal", "fate-of-the-vaal", "Fate+of+the+Vaal"]:
            try:
                r = await client.get(
                    f"https://poe.ninja/poe2/api/builds/{league_slug}/character",
                    params={"account": account_full, "name": "", "overview": league_slug},
                    headers={**NINJA_HEADERS, "Referer": f"https://poe.ninja/poe2/builds/{league_slug}"},
                )
                if r.status_code == 200:
                    cdata = r.json()
                    if cdata and isinstance(cdata, dict) and cdata.get("name"):
                        result["list"].append({
                            "name": cdata.get("name", ""),
                            "class": cdata.get("class", ""),
                            "level": cdata.get("level", 0),
                            "league": cdata.get("league", league_slug),
                            "items": cdata.get("items", []),
                            "skills": cdata.get("skills", []),
                            "character": cdata.get("defensiveStats", {}),
                            "keystones": cdata.get("keystones", []),
                            "source": "poe.ninja",
                        })
            except Exception:
                pass

    if not result["list"]:
        result["note"] = "No PoE2 characters on the poe.ninja ladder yet."
    return result


async def fetch_ninja_character(url: str) -> dict | None:
    """Fetch a single character from a poe.ninja URL.
    URL format: https://poe.ninja/poe2/builds/{league}/character/{account}/{name}
    """
    # Parse the URL
    parts = url.rstrip("/").split("/")
    try:
        idx = parts.index("character")
        account = parts[idx + 1]
        name = parts[idx + 2] if len(parts) > idx + 2 else ""
        # Find league slug (between builds/ and character/)
        builds_idx = parts.index("builds")
        league_slug = parts[builds_idx + 1]
    except (ValueError, IndexError):
        return None

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"https://poe.ninja/poe2/api/builds/{league_slug}/character",
                params={"account": account, "name": name, "overview": league_slug},
                headers={**NINJA_HEADERS, "Referer": url},
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return None


async def poll_all_characters():
    for user in USERS:
        chars = await fetch_characters(user)
        data = load_user(user)
        data["characters"] = chars
        save_user(user, data)
        await asyncio.sleep(5)


async def background_poller():
    while True:
        try:
            await poll_all_characters()
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_poller())
    yield
    task.cancel()

app = FastAPI(title="PoE2 League Companion", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from lib import web as _v2_web  # noqa: E402  (import after app created to avoid cycles)
app.include_router(_v2_web.router)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/api/login", "/api/leagues") or path.startswith("/static"):
        return await call_next(request)
    if not get_user(request):
        if path.startswith("/api/") or path.startswith("/v2/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PoE2 Cockpit — Log In</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:#050302 radial-gradient(ellipse at top,#1a0e06 0%,#050302 55%) fixed;
  color:#d4c5a9;
  font-family:'Inter','Segoe UI',system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;
  overflow:hidden;
}
body::before{
  content:''; position:fixed; inset:0; pointer-events:none;
  background:
    radial-gradient(circle at 20% 30%, rgba(200,120,40,0.12), transparent 40%),
    radial-gradient(circle at 80% 75%, rgba(120,40,20,0.14), transparent 45%);
  z-index:0;
}
.frame{
  position:relative; z-index:1;
  background: linear-gradient(180deg, #120905 0%, #0b0604 100%);
  border:1px solid #3a2818;
  border-radius:4px;
  box-shadow: 0 0 0 1px #080504 inset, 0 0 40px rgba(0,0,0,0.8), 0 0 80px rgba(120,40,20,0.15);
  padding:40px 44px 32px;
  width:360px;
}
.frame::before, .frame::after{
  content:''; position:absolute; width:40px; height:40px;
  border-color:#7a5020; border-style:solid; border-width:0;
  pointer-events:none;
}
.frame::before{
  top:-2px; left:-2px;
  border-top-width:2px; border-left-width:2px;
}
.frame::after{
  bottom:-2px; right:-2px;
  border-bottom-width:2px; border-right-width:2px;
}
.crest{
  text-align:center; margin-bottom:6px;
  font-family:'Cinzel',serif; color:#c8a84b; letter-spacing:5px;
  font-size:11px; text-transform:uppercase;
  text-shadow:0 0 8px rgba(200,168,75,0.25);
}
h1{
  text-align:center;
  font-family:'Cinzel',serif;
  font-weight:700;
  font-size:24px;
  color:#e8d89a;
  letter-spacing:2px;
  margin-bottom:6px;
  text-shadow:0 0 18px rgba(200,168,75,0.3), 0 2px 0 #000;
}
.sub{
  text-align:center; color:#8a7a5a; font-size:11px;
  letter-spacing:2px; text-transform:uppercase; margin-bottom:24px;
}
.divider{
  height:1px; background:linear-gradient(90deg, transparent, #5a4020, transparent);
  margin:0 -12px 20px;
}
label{
  display:block; color:#8a7a5a; font-size:10px;
  text-transform:uppercase; letter-spacing:2px;
  margin-bottom:6px;
}
input{
  display:block; width:100%;
  font-family:inherit; font-size:14px;
  padding:10px 14px; border-radius:3px; margin-bottom:14px;
  background:#181109; border:1px solid #3a2a16; color:#d4c5a9;
  transition:border-color .15s, box-shadow .15s;
}
input:focus{
  border-color:#7a5020; outline:none;
  box-shadow:0 0 0 1px #7a5020, 0 0 12px rgba(200,168,75,0.15);
}
button.primary{
  display:block; width:100%; margin-top:6px;
  background:linear-gradient(180deg,#9a7030,#6a4820);
  border:1px solid #7a5020;
  color:#f5e8c0; font-weight:700; cursor:pointer;
  font-family:'Cinzel',serif; letter-spacing:3px; text-transform:uppercase;
  padding:11px 14px; border-radius:3px; font-size:12px;
  box-shadow:inset 0 1px 0 rgba(255,220,150,0.2), 0 2px 6px rgba(0,0,0,0.5);
  transition:filter .1s, transform .1s;
}
button.primary:hover{ filter:brightness(1.15); }
button.primary:active{ transform:translateY(1px); }
.err{color:#e06050; font-size:12px; margin-bottom:8px; min-height:18px; text-align:center; font-weight:600}
.foot{color:#4a3828; font-size:10px; text-align:center; margin-top:20px; letter-spacing:2px; text-transform:uppercase}
</style></head><body>
<form class="frame" id="login-form" name="poe2cockpit-login" autocomplete="on" onsubmit="event.preventDefault(); login();">
<div class="crest">⚔ · ·  · · ⚔</div>
<h1>Path of Exile II</h1>
<div class="sub">League Cockpit</div>
<div class="divider"></div>
<div class="err" id="err"></div>
<label for="username">Exile name</label>
<input id="username" name="username" placeholder="username" autocomplete="username" autofocus>
<label for="password">Secret</label>
<input id="password" name="password" type="password" placeholder="password" autocomplete="current-password">
<button class="primary" type="submit">Enter</button>
<div class="foot">· poe2.jsb-emr.us ·</div>
</form>
<script>
async function login(){
  const user=document.getElementById('username').value.trim();
  const pw=document.getElementById('password').value;
  const err=document.getElementById('err');
  err.textContent='';
  try {
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:user,password:pw})});
    const d=await r.json();
    if(d.ok)location.href='/';
    else err.textContent=d.error||'Login failed';
  } catch(e) {
    err.textContent='Network error: '+e.message;
  }
}
</script></body></html>"""


@app.get("/login")
async def login_page():
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login")
async def do_login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip().lower()
    password = body.get("password", "")
    user_cfg = USERS.get(username)
    if not user_cfg or not verify_password(password, user_cfg.get("password_hash", "")):
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    token = secrets.token_urlsafe(32)
    VALID_TOKENS[token] = username
    resp = JSONResponse({"ok": True, "user": username})
    resp.set_cookie("poe2_token", token, httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.post("/api/logout")
async def do_logout(request: Request):
    token = request.cookies.get("poe2_token")
    VALID_TOKENS.pop(token, None)
    resp = RedirectResponse("/login")
    resp.delete_cookie("poe2_token")
    return resp


@app.get("/api/whoami")
async def whoami(request: Request):
    user = get_user(request)
    return {"user": user, "poe_account": USERS[user]["poe_account_full"]}


# ---------------------------------------------------------------------------
# Routes — Per-user state (theme, league, market prices, etc.)
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def get_state(request: Request):
    user = get_user(request)
    data = load_user(user)
    return {"state": data.get("state", {})}


@app.post("/api/state")
async def save_state(request: Request):
    user = get_user(request)
    body = await request.json()
    data = load_user(user)
    data["state"] = body.get("state", data.get("state", {}))
    save_user(user, data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Shared Notes (message board)
# ---------------------------------------------------------------------------

@app.get("/api/notes")
async def get_notes():
    shared = load_shared()
    return {"notes": shared.get("notes", [])}


@app.post("/api/notes")
async def add_note(request: Request):
    user = get_user(request)
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "Empty note"}, status_code=400)
    shared = load_shared()
    shared.setdefault("notes", []).insert(0, {
        "id": str(uuid.uuid4())[:8],
        "user": user,
        "text": text,
        "pinned": False,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # Keep last 200 notes
    shared["notes"] = shared["notes"][:200]
    save_shared(shared)
    return {"ok": True}


@app.patch("/api/notes/{note_id}")
async def update_note(note_id: str, request: Request):
    body = await request.json()
    shared = load_shared()
    for note in shared.get("notes", []):
        if note["id"] == note_id:
            if "pinned" in body:
                note["pinned"] = body["pinned"]
            break
    save_shared(shared)
    return {"ok": True}


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str, request: Request):
    user = get_user(request)
    shared = load_shared()
    shared["notes"] = [n for n in shared.get("notes", []) if not (n["id"] == note_id and n["user"] == user)]
    save_shared(shared)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — File Sharing
# ---------------------------------------------------------------------------
FILES_DIR = DATA_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)


@app.get("/api/files")
async def list_files():
    files = []
    for f in sorted(FILES_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file():
            st = f.stat()
            files.append({
                "name": f.name,
                "size": st.st_size,
                "uploaded": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
    return {"files": files}


@app.post("/api/files/upload")
async def upload_file(request: Request):
    from fastapi import UploadFile
    form = await request.form()
    uploaded = form.get("file")
    if not uploaded or not hasattr(uploaded, "filename"):
        return JSONResponse({"error": "No file"}, status_code=400)
    # Sanitize filename
    name = uploaded.filename.replace("/", "_").replace("\\", "_").replace("..", "_")
    if not name:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    dest = FILES_DIR / name
    content = await uploaded.read()
    dest.write_bytes(content)
    return {"ok": True, "name": name, "size": len(content)}


@app.get("/api/files/{filename}")
async def download_file(filename: str):
    from fastapi.responses import FileResponse
    path = FILES_DIR / filename
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(path, filename=filename)


@app.delete("/api/files/{filename}")
async def delete_file(filename: str, request: Request):
    path = FILES_DIR / filename
    if path.exists() and path.is_file():
        path.unlink()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Shared Trade Watchlist
# ---------------------------------------------------------------------------

@app.get("/api/watchlist")
async def get_watchlist():
    shared = load_shared()
    return {"watchlist": shared.get("watchlist", [])}


@app.post("/api/watchlist")
async def add_watchlist_item(request: Request):
    user = get_user(request)
    body = await request.json()
    shared = load_shared()
    shared.setdefault("watchlist", []).insert(0, {
        "id": str(uuid.uuid4())[:8],
        "user": user,
        "for_user": body.get("for_user", ""),  # who this is for ("jbaker", "matt", or "" for both)
        "url": body.get("url", "").strip(),
        "label": body.get("label", "").strip(),
        "note": body.get("note", "").strip(),
        "status": "watching",  # watching, bought, skip
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    save_shared(shared)
    return {"ok": True}


@app.patch("/api/watchlist/{item_id}")
async def update_watchlist_item(item_id: str, request: Request):
    body = await request.json()
    shared = load_shared()
    for item in shared.get("watchlist", []):
        if item["id"] == item_id:
            for k in ("status", "note", "label"):
                if k in body:
                    item[k] = body[k]
            break
    save_shared(shared)
    return {"ok": True}


@app.delete("/api/watchlist/{item_id}")
async def delete_watchlist_item(item_id: str, request: Request):
    shared = load_shared()
    shared["watchlist"] = [w for w in shared.get("watchlist", []) if w["id"] != item_id]
    save_shared(shared)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Shared Builds + Item Checklists
# ---------------------------------------------------------------------------

@app.get("/api/builds")
async def get_builds():
    shared = load_shared()
    return {"builds": shared.get("builds", [])}


@app.post("/api/builds")
async def add_build(request: Request):
    user = get_user(request)
    body = await request.json()
    shared = load_shared()
    build = {
        "id": str(uuid.uuid4())[:8],
        "owner": body.get("owner", user),
        "name": body.get("name", "New Build").strip(),
        "class": body.get("class", "").strip(),
        "guide_terms": body.get("guide_terms", []),  # search terms for guide scanner
        "reference_url": body.get("reference_url", "").strip(),
        "items": body.get("items", []),  # [{name, slot, tag, got_by:{jbaker:bool,matt:bool}, trade_code}]
        "notes": body.get("notes", ""),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    shared.setdefault("builds", []).insert(0, build)
    save_shared(shared)
    return {"ok": True, "build": build}


@app.patch("/api/builds/{build_id}")
async def update_build(build_id: str, request: Request):
    body = await request.json()
    shared = load_shared()
    for build in shared.get("builds", []):
        if build["id"] == build_id:
            for k in ("name", "class", "guide_terms", "reference_url", "items", "notes", "owner"):
                if k in body:
                    build[k] = body[k]
            break
    save_shared(shared)
    return {"ok": True}


@app.delete("/api/builds/{build_id}")
async def delete_build(build_id: str, request: Request):
    shared = load_shared()
    shared["builds"] = [b for b in shared.get("builds", []) if b["id"] != build_id]
    save_shared(shared)
    return {"ok": True}


@app.post("/api/builds/import")
async def import_build(request: Request):
    """Import a build from a poe.ninja character URL — extracts unique items."""
    user = get_user(request)
    body = await request.json()
    url = body.get("url", "").strip()
    if "poe.ninja" not in url:
        return JSONResponse({"error": "Provide a poe.ninja character URL"}, status_code=400)

    cdata = await fetch_ninja_character(url)
    if not cdata or not cdata.get("name"):
        return JSONResponse({"error": "Could not fetch character from poe.ninja. Check the URL."}, status_code=400)

    char_name = cdata.get("name", "")
    char_class = cdata.get("class", "")

    # poe.ninja nests item data under "itemData"
    items = []
    rare_slots = []
    for raw_item in cdata.get("items", []):
        item = raw_item.get("itemData", raw_item)  # handle both formats
        frame = item.get("frameType")
        name = item.get("name", "")
        slot = item.get("inventoryId", "")
        base = item.get("typeLine", "")

        if frame == 3 and name:  # unique
            name = name.replace("<<set:MS>><<set:M>><<set:S>>", "").strip()
            items.append({
                "name": name,
                "slot": slot,
                "tag": "unique",
                "got_by": {"jbaker": False, "matt": False},
                "trade_code": "",
            })
        elif frame == 2 and slot in (
            "Weapon", "Weapon2", "Offhand", "Offhand2", "Helm", "BodyArmour",
            "Gloves", "Boots", "Belt", "Ring", "Ring2", "Amulet"
        ):
            rare_slots.append({
                "name": f"Rare {slot} ({base})",
                "slot": slot,
                "tag": "rare",
                "got_by": {"jbaker": False, "matt": False},
                "trade_code": "",
            })

    all_items = items + rare_slots

    # Generate guide search terms from class/name
    terms = []
    if char_class:
        terms.append(f"poe2 {char_class.lower()} build guide")
    # Try to figure out the main skill from skills data
    skills = cdata.get("skills", [])
    if skills:
        for sg in skills:
            gems = sg if isinstance(sg, list) else sg.get("gems", [])
            for gem in gems:
                gem_data = gem.get("gemData", gem) if isinstance(gem, dict) else gem
                if isinstance(gem_data, dict):
                    is_active = gem_data.get("isActive") or gem_data.get("support") is False
                    gem_name = gem_data.get("name", "")
                    if is_active and gem_name:
                        terms.append(f"poe2 {gem_name.lower()} build")
                        break
            if len(terms) > 1:
                break

    build = {
        "id": str(uuid.uuid4())[:8],
        "owner": body.get("owner", user),
        "name": f"{char_name} ({char_class})" if char_class else char_name,
        "class": char_class,
        "guide_terms": terms,
        "reference_url": url,
        "items": all_items,
        "notes": f"Imported from poe.ninja — {len(items)} uniques, {len(rare_slots)} rare slots",
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    shared = load_shared()
    shared.setdefault("builds", []).insert(0, build)
    save_shared(shared)
    return {"ok": True, "build": build}


# ---------------------------------------------------------------------------
# Routes — Characters
# ---------------------------------------------------------------------------

@app.get("/api/characters")
async def get_characters(request: Request):
    result = {}
    for user, cfg in USERS.items():
        data = load_user(user)
        chars = data.get("characters", {})
        result[user] = {
            "poe_account": cfg["poe_account_full"],
            "last_updated": chars.get("last_updated"),
            "note": chars.get("note"),
            "characters": chars.get("list", []),
        }
    return result


@app.post("/api/poll-characters")
async def poll_characters_now(request: Request):
    user = get_user(request)
    chars = await fetch_characters(user)
    data = load_user(user)
    data["characters"] = chars
    save_user(user, data)
    return {"ok": True, "characters": chars}


# ---------------------------------------------------------------------------
# Routes — Filter Export
# ---------------------------------------------------------------------------

# Map inventory slot IDs to PoE2 filter Class values
SLOT_TO_CLASS = {
    "Weapon": "Wands Sceptres Staves",
    "Weapon2": "Wands Sceptres Staves",
    "Offhand": "Foci Shields",
    "Offhand2": "Foci Shields",
    "Helm": "Helmets",
    "BodyArmour": "Body Armours",
    "Gloves": "Gloves",
    "Boots": "Boots",
    "Belt": "Belts",
    "Ring": "Rings",
    "Ring2": "Rings",
    "Amulet": "Amulets",
}

# Mobalytics itemClassSlug -> PoE2 filter Class
MOBA_CLASS_TO_FILTER = {
    "amulet": "Amulets",
    "belt": "Belts",
    "body-armour": "Body Armours",
    "boots": "Boots",
    "bow": "Bows",
    "crossbow": "Crossbows",
    "focus": "Foci",
    "gloves": "Gloves",
    "helmet": "Helmets",
    "lifeflask": "Life Flasks",
    "manaflask": "Mana Flasks",
    "quiver": "Quivers",
    "ring": "Rings",
    "sceptre": "Sceptres",
    "shield": "Shields",
    "spear": "Spears",
    "talisman": "Amulets",
    "utilityflask": "Charms",
    "wand": "Wands",
    "warstaff": "Quarterstaves",
    "quarterstaff": "Quarterstaves",
}


@app.get("/api/filter/export")
async def export_filter(request: Request):
    """Export all build items as a JSON filter config for the local update script.

    Pulls from both user-defined builds and Mobalytics tracked guides.
    """
    shared = load_shared()
    builds = shared.get("builds", [])
    tracked = shared.get("tracked_guides", [])

    unique_names = set()
    rare_bases = []  # {class, base_type}

    for build in builds:
        for item in build.get("items", []):
            name = item.get("name", "").strip()
            tag = item.get("tag", "")
            slot = item.get("slot", "")

            if tag == "unique" and name:
                unique_names.add(name)
            elif tag == "rare" and slot:
                # Extract base type from "Rare Boots (Luxurious Slippers)"
                import re
                m = re.search(r'\((.+)\)', name)
                if m:
                    rare_bases.append({
                        "class": SLOT_TO_CLASS.get(slot, ""),
                        "base_type": m.group(1),
                    })

    # Merge in items from Mobalytics tracked guides
    for guide in tracked:
        for u in guide.get("uniques", []):
            n = (u.get("name") or "").strip()
            if n:
                unique_names.add(n)
        for r in guide.get("rares", []):
            base = (r.get("base_type") or "").strip()
            cls = MOBA_CLASS_TO_FILTER.get(r.get("class_slug") or "", "")
            if base and cls:
                rare_bases.append({"class": cls, "base_type": base})

    filter_data = {
        "version": "1",
        "generated": datetime.now(timezone.utc).isoformat(),
        "builds": [b["name"] for b in builds] + [f"[Moba] {g['name']}" for g in tracked],
        "rules": [],
    }

    # Rule 1: Highlight build-defining uniques
    if unique_names:
        filter_data["rules"].append({
            "comment": "Build-defining unique items",
            "action": "Show",
            "conditions": {
                "Rarity": "Unique",
                "BaseType": sorted(unique_names),
            },
            "style": {
                "SetFontSize": 45,
                "SetTextColor": "255 165 0 255",
                "SetBorderColor": "255 165 0 255",
                "SetBackgroundColor": "60 30 0 255",
                "PlayEffect": "Orange",
                "MinimapIcon": "0 Orange Star",
                "PlayAlertSoundPositional": "1 300",
            },
        })

    # Rule 2: Highlight specific rare bases we want
    # Group by class for cleaner rules
    class_bases: dict[str, list[str]] = {}
    for rb in rare_bases:
        cls = rb["class"]
        bt = rb["base_type"]
        if cls and bt:
            class_bases.setdefault(cls, []).append(bt)

    for cls, bases in class_bases.items():
        filter_data["rules"].append({
            "comment": f"Target rare bases: {cls}",
            "action": "Show",
            "conditions": {
                "Rarity": "Rare",
                "Class": cls.split(),
                "BaseType": sorted(set(bases)),
            },
            "style": {
                "SetFontSize": 40,
                "SetTextColor": "255 255 100 255",
                "SetBorderColor": "255 255 100 255",
                "SetBackgroundColor": "40 40 0 255",
                "PlayEffect": "Yellow Temp",
                "MinimapIcon": "1 Yellow Diamond",
            },
        })

    return filter_data


@app.get("/api/filter/export/download")
async def download_filter_json(request: Request):
    """Download the filter config as a .json file."""
    from fastapi.responses import Response
    data = await export_filter(request)
    content = json.dumps(data, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=poe2-filter-rules.json"},
    )


# ---------------------------------------------------------------------------
# Routes — Tracked Mobalytics Guides
# ---------------------------------------------------------------------------

MOBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html",
}


async def scrape_mobalytics_build(url: str) -> dict:
    """Fetch a Mobalytics PoE2 build page and extract uniques + rare bases.

    Returns {name, uniques: [{name, class_slug}], rares: [{name, base_type, class_slug}]}.
    Raises on network/parse failure.
    """
    import re as _re

    if "mobalytics.gg/poe-2/builds/" not in url:
        raise ValueError("Not a Mobalytics PoE2 build URL")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, headers=MOBA_HEADERS)
        r.raise_for_status()
        html = r.text

    # The build page embeds Apollo state with `commonItem` blocks per slot.
    # Each block includes slug, isUnique flag, name, and itemClassSlug.
    pattern = _re.compile(
        r'"commonItem":\{[^{]*?"slug":"([^"]+)"[^{]*?'
        r'"isUnique":(true|false)[^{]*?'
        r'"name":"([^"]+)"[^{]*?'
        r'"itemClassSlug":"([^"]+)"'
    )

    uniques: list[dict] = []
    rares: list[dict] = []
    seen_unique = set()
    seen_rare = set()

    for m in pattern.finditer(html):
        slug, is_unique, name, class_slug = m.groups()
        # Mobalytics escapes apostrophes as \u0027 in JSON
        name = name.encode().decode("unicode_escape")
        if is_unique == "true":
            key = name.lower()
            if key not in seen_unique:
                seen_unique.add(key)
                uniques.append({"name": name, "class_slug": class_slug})
        else:
            key = (name.lower(), class_slug)
            if key not in seen_rare:
                seen_rare.add(key)
                rares.append({
                    "name": name,
                    "base_type": name,  # for rares the "name" field IS the base type
                    "class_slug": class_slug,
                })

    # Try to grab the build title from the page <title> or og:title
    title_m = _re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if title_m:
        page_name = title_m.group(1).split("|")[0].strip()
    else:
        slug = url.rstrip("/").split("/")[-1]
        page_name = slug.replace("-", " ").title()

    return {"name": page_name, "uniques": uniques, "rares": rares}


@app.get("/api/market/tracked")
async def list_tracked_guides(request: Request):
    if not get_user(request):
        return JSONResponse({"error": "auth required"}, status_code=401)
    shared = load_shared()
    return {"tracked": shared.get("tracked_guides", [])}


@app.post("/api/market/tracked")
async def add_tracked_guide(request: Request):
    user = get_user(request)
    if not user:
        return JSONResponse({"error": "auth required"}, status_code=401)
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)

    shared = load_shared()
    tracked = shared.setdefault("tracked_guides", [])

    # Reject duplicates
    if any(g.get("url") == url for g in tracked):
        return JSONResponse({"error": "already tracked"}, status_code=409)

    try:
        scraped = await scrape_mobalytics_build(url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"scrape failed: {e}"}, status_code=502)

    entry = {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "name": scraped["name"],
        "uniques": scraped["uniques"],
        "rares": scraped["rares"],
        "added_by": user,
        "added_ts": int(time.time()),
    }
    tracked.append(entry)
    save_shared(shared)
    return entry


@app.delete("/api/market/tracked/{guide_id}")
async def delete_tracked_guide(guide_id: str, request: Request):
    if not get_user(request):
        return JSONResponse({"error": "auth required"}, status_code=401)
    shared = load_shared()
    tracked = shared.get("tracked_guides", [])
    new = [g for g in tracked if g.get("id") != guide_id]
    if len(new) == len(tracked):
        return JSONResponse({"error": "not found"}, status_code=404)
    shared["tracked_guides"] = new
    save_shared(shared)
    return {"ok": True}


@app.post("/api/market/tracked/{guide_id}/refresh")
async def refresh_tracked_guide(guide_id: str, request: Request):
    if not get_user(request):
        return JSONResponse({"error": "auth required"}, status_code=401)
    shared = load_shared()
    tracked = shared.get("tracked_guides", [])
    for g in tracked:
        if g.get("id") == guide_id:
            try:
                scraped = await scrape_mobalytics_build(g["url"])
            except Exception as e:
                return JSONResponse({"error": f"scrape failed: {e}"}, status_code=502)
            g["name"] = scraped["name"]
            g["uniques"] = scraped["uniques"]
            g["rares"] = scraped["rares"]
            g["refreshed_ts"] = int(time.time())
            save_shared(shared)
            return g
    return JSONResponse({"error": "not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Routes — Market Intelligence
# ---------------------------------------------------------------------------

@app.get("/api/market/meta")
async def meta_builds(request: Request):
    """Scrape Mobalytics tier list + build guides for S/A tier meta info."""
    import re as _re

    browser_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html",
    }

    result = {"tier_list": [], "builds": [], "error": None}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # 1. Scrape tier list page
        try:
            r = await client.get("https://mobalytics.gg/poe-2/tier-list", headers=browser_headers)
            if r.status_code == 200:
                html = r.text
                # Extract tier sections — look for tier headings followed by ascendancy names
                # The page has structured tier data we can parse
                tiers = _re.findall(
                    r'(?:"|>)\s*(S|A|B|C|D|F)\s*(?:-|\s)?[Tt]ier',
                    html,
                )
                # Extract ascendancy names near tier markers
                # Simplified: use known structure from WebFetch results
                # We'll maintain a static mapping updated periodically
                pass
        except Exception:
            pass

        # 2. Scrape builds page for guide links
        try:
            r = await client.get("https://mobalytics.gg/poe-2/builds", headers=browser_headers)
            if r.status_code == 200:
                html = r.text
                slugs = _re.findall(r'href="(/poe-2/builds/([a-z0-9][a-z0-9-]+))"', html)
                for href, slug in slugs:
                    # Title-case the slug for display: "fubgun-poisonburst-pathfinder"
                    # → "Fubgun Poisonburst Pathfinder"
                    name = slug.replace("-", " ").title()
                    result["builds"].append({
                        "name": name,
                        "url": f"https://mobalytics.gg{href}",
                        "slug": slug,
                    })
        except Exception as e:
            result["error"] = str(e)

    # Static tier list (scraped via WebFetch, updated periodically).
    # Tier rankings are 0.4 Mobalytics carryover — 0.5 "Return of the Ancients"
    # just launched, so the 0.5 meta has not yet settled. Class labels reflect
    # the 0.5 ascendancy/base-class lineup. The two new-in-0.5 ascendancies
    # (Martial Artist, Spirit Walker) are listed as tier "?" until rankings
    # stabilize. The Ascendancy Changes section of 0.5.0 also rebalanced
    # Acolyte of Chayula, Blood Mage, Chronomancer, Gemling Legionnaire,
    # Pathfinder, and Witchhunter — their pre-0.5 tier placements may shift.
    result["tier_list"] = [
        {"tier": "S", "name": "Pathfinder", "class": "Ranger"},
        {"tier": "S", "name": "Amazon", "class": "Huntress"},
        {"tier": "A", "name": "Deadeye", "class": "Ranger"},
        {"tier": "A", "name": "Lich", "class": "Witch"},
        {"tier": "A", "name": "Titan", "class": "Warrior"},
        {"tier": "A", "name": "Blood Mage", "class": "Witch"},
        {"tier": "A", "name": "Tactician", "class": "Mercenary"},
        {"tier": "A", "name": "Witchhunter", "class": "Mercenary"},
        {"tier": "B", "name": "Warbringer", "class": "Warrior"},
        {"tier": "B", "name": "Shaman", "class": "Druid"},
        {"tier": "B", "name": "Infernalist", "class": "Witch"},
        {"tier": "B", "name": "Invoker", "class": "Monk"},
        {"tier": "C", "name": "Smith of Kitava", "class": "Warrior"},
        {"tier": "C", "name": "Stormweaver", "class": "Sorceress"},
        {"tier": "D", "name": "Ritualist", "class": "Huntress"},
        {"tier": "F", "name": "Acolyte of Chayula", "class": "Monk"},
        {"tier": "F", "name": "Oracle", "class": "Druid"},
        {"tier": "F", "name": "Gemling Legionnaire", "class": "Mercenary"},
        {"tier": "F", "name": "Chronomancer", "class": "Sorceress"},
        {"tier": "?", "name": "Martial Artist", "class": "Monk", "new": True},
        {"tier": "?", "name": "Spirit Walker", "class": "Huntress", "new": True},
    ]
    result["note"] = (
        "Tier rankings carry over from Mobalytics 0.4 — 0.5 'Return of the Ancients' "
        "meta still settling. New 0.5 ascendancies (Martial Artist ★, Spirit Walker ★) "
        "shown as '?'. Pre-0.5 rankings for Acolyte of Chayula, Blood Mage, Chronomancer, "
        "Gemling Legionnaire, Pathfinder, and Witchhunter may shift due to 0.5 balance changes."
    )
    result["tier_list_url"] = "https://mobalytics.gg/poe-2/tier-list"

    # Deduplicate builds
    seen = set()
    unique_builds = []
    for b in result["builds"]:
        slug = b.get("slug", b.get("url", ""))
        if slug not in seen:
            seen.add(slug)
            unique_builds.append(b)
    result["builds"] = unique_builds

    return result


# ---------------------------------------------------------------------------
# Routes — Proxies
# ---------------------------------------------------------------------------

@app.get("/api/leagues")
async def proxy_leagues():
    async with httpx.AsyncClient(headers=POE_HEADERS, timeout=15) as client:
        r = await client.get(f"{POE_BASE}/api/trade2/data/leagues")
        if r.status_code == 200:
            return r.json()
    return {"result": []}


@app.get("/api/reddit/search")
async def proxy_reddit(sub: str, q: str, t: str = "week", limit: int = 15):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://www.reddit.com/r/{sub}/search.json",
            params={"q": q, "restrict_sr": "on", "sort": "new", "t": t, "limit": limit, "type": "link"},
            headers={"User-Agent": "PoE2-Tracker/1.0"},
        )
        if r.status_code == 200:
            return r.json()
    return {"data": {"children": []}}


@app.get("/api/ninja/currency")
async def proxy_ninja_currency(league: str = "Standard"):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://poe.ninja/api/data/currencyoverview",
            params={"league": league, "type": "Currency"},
            headers={"User-Agent": "PoE2-Tracker/1.0", "Accept": "application/json"},
        )
        if r.status_code == 200:
            return r.json()
    return {"lines": []}


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return HTMLResponse((STATIC_DIR / "cockpit.html").read_text())


@app.get("/v2")
async def v2_legacy():
    # /v2 was the original cockpit mount point; now the default. Keep an
    # alias so older bookmarks still work.
    return RedirectResponse("/", status_code=301)
