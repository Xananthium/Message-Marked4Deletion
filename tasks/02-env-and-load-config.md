---
id: 02
title: Write .env.example and implement load_config() in poller.py
platform: BACKEND
depends_on: []
files_touched: [/home/discnxt/aib/.env.example, /home/discnxt/aib/poller.py, /home/discnxt/aib/requirements.txt]
estimate_minutes: 30
estimate_loc: 80
---

## Description
Stand up `poller.py` skeleton with a `Config` dataclass and `load_config()` that reads `.env` (the systemd unit uses `EnvironmentFile=`, so values arrive as `os.environ` at runtime). Author `.env.example` with every variable consumed by poller.py and provision-site.sh. Pin runtime dependencies in `requirements.txt`.

## Implementation notes
- `.env.example` ≤ 20 lines. Required keys: `DSN` (e.g. `postgresql:///agentinabox`), `GOOGLE_SA_PATH=/home/discnxt/.secrets/google-agents.json`, `MAILBOX=team@digitaldisconnections.com`, `OPERATOR_EMAIL=cass@digitaldisconnections.com`, `CONTABO_SSH=contabo`, `AIDER_MODEL=ollama_chat/kimi-k2.6:cloud`, `TMP_ROOT=/tmp/aib`, plus Namecheap keys consumed by provision-site.sh: `NAMECHEAP_API_USER`, `NAMECHEAP_API_KEY`, `NAMECHEAP_USERNAME`, `NAMECHEAP_CLIENT_IP`, `CONTABO_IP`.
- `requirements.txt`: exactly three lines — `google-api-python-client`, `psycopg[binary]`, `aider-chat`. (`requests` is a transitive dep of google-api-python-client; do not pin separately.)
- In `poller.py`: top-of-file imports (stdlib only at this stage — `os`, `sys`, `dataclasses`, `pathlib`, `tempfile`, `subprocess`, `shutil`, `base64`, `email.utils`, `email.message`, `logging`), then:
  - `@dataclass(frozen=True) class Config:` fields `dsn: str, sa_path: str, mailbox: str, operator_email: str, ssh_alias: str, model: str, tmp_root: str`.
  - `def load_config() -> Config:` reads each key from `os.environ`; raises `RuntimeError(f"missing env: {key}")` if any required key is absent. `tmp_root` is created via `os.makedirs(tmp_root, exist_ok=True)` before returning.
- Module-level `log = logging.getLogger("aib")` and `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")` in `main()` (defer main impl to task 06; for now `if __name__ == "__main__": load_config()` so the file is importable and runnable).

## Acceptance criteria
- [ ] `.env.example` lists every key consumed by poller.py + provision-site.sh and is ≤20 lines
- [ ] `requirements.txt` contains exactly the three approved packages
- [ ] `python3 poller.py` with a populated `.env` exits 0; with a missing key, exits non-zero and prints which key
- [ ] `Config` is a frozen dataclass with all 7 fields from ARCHITECTURE.md
- [ ] No TODO comments
- [ ] All error paths handled (missing env raises with the key name)
- [ ] No placeholder functions or fake data
