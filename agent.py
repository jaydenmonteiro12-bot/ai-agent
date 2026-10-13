#!/usr/bin/env python3
"""Personal agent v0.5.2: adds write_file, copy_from_reference, list_repo_files."""
import json, os, sys, base64, hashlib, subprocess, shutil
from datetime import datetime
from urllib.parse import urlparse
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "ai-agent")
WORK = os.path.join(BASE, "work")
REPOS = os.path.join(BASE, "repos")
REFS = os.path.join(BASE, "refs")
VAULT = os.path.join(BASE, "vault.enc")
LOGENC = os.path.join(BASE, "session.log.enc")
KEYFILE = os.path.join(BASE, ".agent.key")
OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"
MAX_TURNS = 10

SYSTEM = """You are Jayden's private local assistant. Tools available:

- list_files (no args): files in the work folder
- read_file {"path": "..."}: one file from the work folder
- read_notes (no args): Jayden's saved memory notes
- read_topic {"topic": "..."}: read one structured learning topic
- save_topic {"topic": "...", "field": "...", "content": "..."}: fields are
  question, definitions, explanation, method, result, unclear, next_action
- append_note {"text": "..."}: save a short free-form note
- get_time (no args): current date and time
- git_status {"repo": "name"}: branch and changed files for one of Jayden's repos
- list_repo_files {"repo": "name"}: files inside one of Jayden's repos
- git_commit {"repo": "name", "message": "..."}: stage all and commit
- git_push {"repo": "name"}: push to GitHub (always needs confirmation)
- clone_repo {"url": "..."}: clone a PUBLIC github.com repo into the reference
  library for study (always needs confirmation)
- list_repos (no args): reference repos available
- list_reference_files {"repo": "name"}: files inside a reference repo
- read_reference {"repo": "name", "path": "..."}: read one file from a reference repo
- write_file {"where": "work" or "repo", "repo": "name", "path": "...",
  "content": "..."}: create or overwrite a file. ALWAYS needs confirmation.
- copy_from_reference {"ref": "name", "path": "...", "repo": "name",
  "dest_path": "..."}: copy one file from a reference repo into one of Jayden's
  repos. ALWAYS needs confirmation. Remind Jayden to check the licence.

Tool choice rules:
- "what notes..." -> read_notes; "remember..." -> append_note
- learning/study material -> save_topic or read_topic
- "what time is it" -> get_time
- "commit" -> git_commit; "push" -> git_push
- "clone..." -> clone_repo; study/reference questions -> the reference tools
- "create"/"write"/"edit"/"fix this file" -> write_file
- "copy that file into my project" -> copy_from_reference
- Never use save tools to answer questions. Never guess.

Rules:
- Jayden's repos live in the repos folder: never force-push, never commit
  files ending in .token or .key.
- Reference repos are READ-ONLY. The only way to move code out of them is
  copy_from_reference, and Jayden approves each copy.
- write_file overwrites the whole file: state clearly in your confirmation
  flow which file and roughly how many lines you are writing.
- When answering from a tool result, use exactly what it says. Do not invent
  details that are not in it. If the request is ambiguous, ask first.

To use a tool, reply with ONLY one JSON object:
{"action":"tool","tool":"NAME","args":{...}}

When done, reply with:
{"action":"reply","text":"your answer"}"""

KNOWN = ("list_files", "read_file", "read_notes", "read_topic", "save_topic",
         "append_note", "get_time", "git_status", "git_commit", "git_push",
         "clone_repo", "list_repos", "list_reference_files", "read_reference",
         "list_repo_files", "write_file", "copy_from_reference")

def load_keyfile():
    if not os.path.exists(KEYFILE):
        key = Fernet.generate_key()
        with open(KEYFILE, "wb") as f:
            f.write(key)
        os.chmod(KEYFILE, 0o600)
    with open(KEYFILE, "rb") as f:
        return Fernet(f.read())

def enc_write(fer, path, obj):
    with open(path, "wb") as f:
        f.write(fer.encrypt(json.dumps(obj).encode()))

def enc_read(fer, path, default):
    if not os.path.exists(path):
        return default
    with open(path, "rb") as f:
        return json.loads(fer.decrypt(f.read()).decode())

def safe_path(rel):
    p = os.path.realpath(os.path.join(WORK, rel))
    if not p.startswith(os.path.realpath(WORK) + os.sep):
        raise ValueError("path outside work folder")
    return p

def repo_path(name):
    name = str(name).strip().strip("/.")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid repo name")
    p = os.path.realpath(os.path.join(REPOS, name))
    if not p.startswith(os.path.realpath(REPOS) + os.sep):
        raise ValueError("repo outside repos folder")
    if not os.path.exists(os.path.join(p, ".git")):
        raise ValueError("not a git repo (clone it first)")
    return p

def ref_path(name):
    name = str(name).strip().strip("/.")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid reference repo name")
    p = os.path.realpath(os.path.join(REFS, name))
    if not p.startswith(os.path.realpath(REFS) + os.sep):
        raise ValueError("reference repo outside refs folder")
    if not os.path.isdir(p):
        raise ValueError("not cloned yet")
    return p

def run_git(repo, *args):
    r = subprocess.run(["git", "-C", repo_path(repo), *args],
                       capture_output=True, text=True, timeout=60)
    return f"exit={r.returncode}\n{(r.stdout + r.stderr).strip()[:1500]}"

def tool_list_files():
    return "\n".join(os.listdir(WORK)) or "(empty)"

def tool_read_file(rel):
    with open(safe_path(rel)) as f:
        return f.read()[:4000]

def tool_read_notes(fer):
    data = enc_read(fer, VAULT, {"notes": [], "topics": {}})
    out = [f"{n['date'][:16]}: {n['note']}" for n in data["notes"][-15:]]
    return "\n".join(out) or "(no notes)"

def tool_append_note(fer, text):
    data = enc_read(fer, VAULT, {"notes": [], "topics": {}})
    data["notes"].append({"date": datetime.now().isoformat(), "note": str(text)[:500]})
    data["notes"] = data["notes"][-200:]
    enc_write(fer, VAULT, data)
    return f"note saved ({len(data['notes'])} total)"

def tool_read_topic(fer, topic):
    data = enc_read(fer, VAULT, {"notes": [], "topics": {}})
    t = data["topics"].get(str(topic).lower())
    if not t:
        return f"(no topic: {topic})"
    return "\n".join(f"{k}: {v}" for k, v in t.items() if k != "created") or "(empty topic)"

def tool_save_topic(fer, topic, field, content):
    data = enc_read(fer, VAULT, {"notes": [], "topics": {}})
    t = data["topics"].setdefault(str(topic).lower(), {})
    t["created"] = t.get("created", datetime.now().isoformat())
    t[str(field)[:40]] = str(content)[:800]
    enc_write(fer, VAULT, data)
    return f"saved {field} for topic '{topic}'"

def tool_get_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M %A")

def tool_git_status(repo):
    return run_git(repo, "status", "--short", "--branch")

def tool_list_repo_files(repo):
    base = repo_path(repo)
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), base))
    return "\n".join(out[:200]) or "(empty)"

def tool_git_commit(repo, message):
    return run_git(repo, "add", "-A") + "\n" + run_git(repo, "commit", "-m", str(message)[:200])

def tool_git_push(repo):
    return run_git(repo, "push", "origin", "HEAD")

def tool_write_file(where, repo, path, content):
    if str(where) == "work":
        dest = safe_path(path)
    elif str(where) == "repo":
        dest = os.path.join(repo_path(repo), str(path).lstrip("/"))
        if not dest.startswith(os.path.realpath(repo_path(repo)) + os.sep):
            raise ValueError("path outside repo")
    else:
        raise ValueError("where must be 'work' or 'repo'")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(str(content))
    lines = str(content).count("\n") + 1
    return f"wrote {lines} lines to {os.path.relpath(dest, BASE)}"

def tool_copy_from_reference(ref, path, repo, dest_path):
    src = os.path.realpath(os.path.join(ref_path(ref), str(path)))
    if not src.startswith(os.path.realpath(ref_path(ref)) + os.sep):
        raise ValueError("source path outside reference repo")
    dest = os.path.join(repo_path(repo), str(dest_path).lstrip("/"))
    if not dest.startswith(os.path.realpath(repo_path(repo)) + os.sep):
        raise ValueError("destination outside repo")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)
    return f"copied {path} from {ref} into {repo}/{dest_path} (check the licence before committing)"

def tool_clone_repo(url):
    u = urlparse(str(url))
    if u.scheme != "https" or u.hostname != "github.com":
        return "blocked: only public https://github.com URLs"
    parts = [p for p in u.path.split("/") if p]
    if len(parts) != 2:
        return "blocked: URL must look like https://github.com/owner/repo"
    name = parts[1].replace(".git", "")
    dest = os.path.join(REFS, name)
    if os.path.exists(dest):
        return f"already cloned: {name}"
    r = subprocess.run(["git", "clone", "--depth", "1", str(url), dest],
                       capture_output=True, text=True, timeout=180)
    return f"exit={r.returncode}\n{(r.stdout + r.stderr).strip()[:800]}"

def tool_list_repos():
    return "\n".join(os.listdir(REFS)) or "(no reference repos cloned)"

def tool_list_reference_files(repo):
    base = ref_path(repo)
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), base))
    return "\n".join(out[:200]) or "(empty)"

def tool_read_reference(repo, path):
    base = ref_path(repo)
    p = os.path.realpath(os.path.join(base, str(path)))
    if not p.startswith(os.path.realpath(base) + os.sep):
        raise ValueError("path outside reference repo")
    with open(p, "r", errors="replace") as f:
        return f.read()[:4000]

def log_encrypted(fer, text):
    logdata = enc_read(fer, LOGENC, [])
    logdata.append({"ts": datetime.now().isoformat(), "text": text})
    enc_write(fer, LOGENC, logdata[-300:])

DISPATCH = {
    "list_files": lambda fer, a: tool_list_files(),
    "read_file": lambda fer, a: tool_read_file(a.get("path", "")),
    "read_notes": lambda fer, a: tool_read_notes(fer),
    "read_topic": lambda fer, a: tool_read_topic(fer, a.get("topic", "")),
    "save_topic": lambda fer, a: tool_save_topic(fer, a.get("topic", ""), a.get("field", ""), a.get("content", "")),
    "append_note": lambda fer, a: tool_append_note(fer, a.get("text", "")),
    "get_time": lambda fer, a: tool_get_time(),
    "git_status": lambda fer, a: tool_git_status(a.get("repo", "")),
    "list_repo_files": lambda fer, a: tool_list_repo_files(a.get("repo", "")),
    "git_commit": lambda fer, a: tool_git_commit(a.get("repo", ""), a.get("message", "")),
    "git_push": lambda fer, a: tool_git_push(a.get("repo", "")),
    "clone_repo": lambda fer, a: tool_clone_repo(a.get("url", "")),
    "list_repos": lambda fer, a: tool_list_repos(),
    "list_reference_files": lambda fer, a: tool_list_reference_files(a.get("repo", "")),
    "read_reference": lambda fer, a: tool_read_reference(a.get("repo", ""), a.get("path", "")),
    "write_file": lambda fer, a: tool_write_file(a.get("where", ""), a.get("repo", ""), a.get("path", ""), a.get("content", "")),
    "copy_from_reference": lambda fer, a: tool_copy_from_reference(a.get("ref", ""), a.get("path", ""), a.get("repo", ""), a.get("dest_path", "")),
}
WRITE_TOOLS = {"save_topic", "append_note", "git_commit", "git_push",
               "write_file", "copy_from_reference"}
ALWAYS_CONFIRM = {"git_push", "clone_repo", "write_file", "copy_from_reference"}
AUTO_IN_ASK = {"list_files", "get_time", "read_notes", "read_topic", "git_status",
               "list_repos", "list_reference_files", "read_reference", "list_repo_files"}

def main():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(REPOS, exist_ok=True)
    os.makedirs(REFS, exist_ok=True)
    fer = load_keyfile()
    mode = "ask"
    if len(sys.argv) > 1 and sys.argv[1] == "--read":
        mode = "read"
    print(f"Agent v0.5.2 | mode: {mode} | 'exit' to quit.")
    messages = [{"role": "system", "content": SYSTEM}]
    actions = []
    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in ("exit", "quit"):
            break
        messages.append({"role": "user", "content": user})
        for turn in range(MAX_TURNS):
            reply = requests.post(OLLAMA, json={
                "model": MODEL, "messages": messages, "stream": False,
                "options": {"temperature": 0}},
                timeout=120).json()["message"]["content"].strip()
            cleaned = reply
            if "{" in cleaned and "}" in cleaned:
                cleaned = cleaned[cleaned.index("{"):cleaned.rindex("}") + 1]
            try:
                act = json.loads(cleaned)
            except json.JSONDecodeError:
                print("agent>", reply)
                break
            if act.get("action") == "reply":
                print("agent>", act.get("text", ""))
                break
            name = act.get("tool") or act.get("action")
            args = act.get("args") or {k: v for k, v in act.items()
                                       if k not in ("action", "tool")}
            if name not in KNOWN:
                print("debug> raw model reply:", reply)
                result = "unknown tool"
            else:
                if name in ALWAYS_CONFIRM:
                    confirmed = False
                elif name in WRITE_TOOLS and mode != "read":
                    confirmed = False
                elif mode == "read" or name in AUTO_IN_ASK:
                    confirmed = True
                else:
                    confirmed = False
                if not confirmed:
                    print(f"[tool: {name} {json.dumps(args, default=str)[:600]}]  run this? (y/n)")
                    confirmed = input("> ").strip().lower() == "y"
                if confirmed:
                    try:
                        result = DISPATCH[name](fer, args)
                    except Exception as e:
                        result = f"error: {e}"
                else:
                    result = "user declined"
            actions.append(f"{name}: {str(result)[:120]}")
            messages.append({"role": "tool", "content": str(result)})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary = f"Session {ts} (mode {mode})\n" + "\n".join(actions[-10:])
    log_encrypted(fer, summary)
    tool_append_note(fer, f"session {ts}: {summary[:300]}")
    print("\n--- encrypted log + note saved ---")

if __name__ == "__main__":
    main()
