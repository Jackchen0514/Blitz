import json
import threading
from pathlib import Path

USERS_DB_PATH = Path("/etc/hysteria/users_data.json")


class Database:
    def __init__(self, db_path=None):
        self._path = Path(db_path) if db_path else USERS_DB_PATH
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text('{}')

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self, data: dict):
        self._path.write_text(json.dumps(data, indent=2))

    def add_user(self, user_data: dict):
        user_data = dict(user_data)
        username = user_data.pop('username', None) or user_data.pop('_id', None)
        if not username:
            raise ValueError("Username is required")
        username = username.lower()
        with self._lock:
            data = self._load()
            if username in data:
                return None
            data[username] = user_data
            self._save(data)
        return True

    def get_user(self, username: str):
        data = self._load()
        user = data.get(username.lower())
        if user is None:
            return None
        return {'_id': username.lower(), **user}

    def get_all_users(self) -> list:
        data = self._load()
        return [{'_id': k, **v} for k, v in data.items()]

    def list_users(self) -> list:
        return self.get_all_users()

    def update_user(self, username: str, updates: dict):
        username = username.lower()
        with self._lock:
            data = self._load()
            if username not in data:
                return False
            data[username].update(updates)
            self._save(data)
        return True

    def unset_user_fields(self, username: str, fields: list):
        """Remove specific fields from a user document."""
        username = username.lower()
        with self._lock:
            data = self._load()
            if username not in data:
                return False
            for field in fields:
                data[username].pop(field, None)
            self._save(data)
        return True

    def insert_user(self, user_data: dict):
        """Insert a user document that already has '_id' (used for renames)."""
        user_data = dict(user_data)
        username = user_data.pop('_id')
        with self._lock:
            data = self._load()
            data[username] = user_data
            self._save(data)
        return True

    def insert_users(self, users_list: list):
        """Bulk insert users. Each doc must have '_id'."""
        with self._lock:
            data = self._load()
            for user_doc in users_list:
                doc = dict(user_doc)
                username = doc.pop('_id')
                data[username] = doc
            self._save(data)
        return True

    def find_users_by_ids(self, usernames: list) -> set:
        """Return set of usernames that exist in the database."""
        data = self._load()
        return {u for u in usernames if u in data}

    def find_user_by_password(self, password: str):
        """Find a user by password, return username or None."""
        data = self._load()
        for username, user in data.items():
            if user.get('password') == password:
                return username
        return None

    def upsert_user(self, username: str, user_data: dict):
        """Insert or update a user document."""
        username = username.lower()
        doc = dict(user_data)
        doc.pop('_id', None)
        with self._lock:
            data = self._load()
            if username in data:
                data[username].update(doc)
            else:
                data[username] = doc
            self._save(data)
        return True

    def delete_user(self, username: str):
        username = username.lower()
        with self._lock:
            data = self._load()
            if username in data:
                del data[username]
                self._save(data)
                return True
        return False

    def delete_users(self, usernames: list):
        usernames_lower = [u.lower() for u in usernames]
        with self._lock:
            data = self._load()
            for u in usernames_lower:
                data.pop(u, None)
            self._save(data)
        return True


try:
    db = Database()
except Exception:
    db = None
