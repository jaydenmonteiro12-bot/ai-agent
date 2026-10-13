#!/usr/bin/env python3
"""Personal agent v0.5: local, encrypted memory, GitHub via SSH, permission ladder."""
import json, os, sys, base64, hashlib, subprocess
from datetime import datetime
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "ai-agent")
WORK = os.path.join(BASE, "work")
REPOS = os.path.join(BASE, "repos")
VAULT = os.path.join(BASE, "vault.enc")
LOGENC = os.path.join(BASE, "session.log.enc")
KEYFILE = os.path.join(BASE, ".agent.key")
OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"
MAX_TURNS = 8

SYSTEM = """You are Jayden's private local assistant. Tools available:

- list_files (no args): files in the work folder
- read_file {"path": "..."}: one file from the work folder
- read_notes (no args): Jayden's saved memory notes
- read_topic {"topic": "..."}: read one structured learning topic
- save_topic {"topic": "...", "field": "...", "content": "..."}: fields are
  question, definitions, explanation, method, result, unclear, next_action
- append_note {"text": "..."}: save a short free-form note
- get_time (no args): current date and time
- git_status {"repo": "name"}: branch and changed files for a repo
- git_commit {"repo": "name", "message": "..."}: stage all changes and commit
- git_push {"repo": "name"}: push to GitHub (always needs confirmation)

Tool choice rules:
- "what notes..." -> read_notes; "remember..." -> append_note
- learning/study material -> save_topic or read_topic
- "what time is it" -> get_time
- "commit"/"save my code" -> git_commit; "push"/"upload to GitHub" -> git_push
- Never use save tools to answer questions. Never guess.

Git rules: only repos inside the repos folder. Never force-push. Never commit
files ending in .token or .key, and never mention their contents.

When answering from a tool result, use exactly what it says. Do not invent
details that are not in it. If the request is ambiguous, ask first.

To use a tool, reply with ONLY one JSON object:
{"action":"tool","tool":"NAME","args":{...}}

When done, reply with:
{"action":"reply","text":"your answer"}"""

KNOWN = ("list_files", "read_file", "read_notes", "read_topic", "save_topic",
         "append_note", "get_time", "git_status", "git_commit", "git_push")

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

def tool_git_commit(repo, message):
    out1 = run_git(repo, "add", "-A")
    out2 = run_git(repo, "commit", "-m", str(message)[:200])
    return f"{out1}\n{out2}"

def tool_git_push(repo):
    return run_git(repo, "push", "origin", "HEAD")

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
    "git_commit": lambda fer, a: tool_git_commit(a.get("repo", ""), a.get("message", "")),
    "git_push": lambda fer, a: tool_git_push(a.get("repo", "")),
}
WRITE_TOOLS = {"save_topic", "append_note", "git_commit", "git_push"}
ALWAYS_CONFIRM = {"git_push"}

def main():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(REPOS, exist_ok=True)
    fer = load_keyfile()
    mode = "ask"
    if len(sys.argv) > 1 and sys.argv[1] == "--read":
        mode = "read"
    print(f"Agent v0.5 | mode: {mode} | 'exit' to quit.")
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
            try:
                act = json.loads(reply)
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
                else:
                    confirmed = True if mode == "read" else False
                if mode == "ask" and name in ("list_files", "get_time",
                                              "read_notes", "read_topic",
                                              "git_status"):
                    confirmed = True
                elif name in WRITE_TOOLS or name in ALWAYS_CONFIRM:
                    print(f"[tool: {name} {json.dumps(args)}]  run this? (y/n)")
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
