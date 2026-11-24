"""A stand-in for paramiko so Beacon's own code can be tested with no SSH library and no devices.
It copies only the parts of paramiko's interface that app.py uses: the policy base class, HostKeys
(a known_hosts file), BadHostKeyException and SSHClient."""
import base64


class BadHostKeyException(Exception):
    def __init__(self, hostname, got_key, expected_key):
        super().__init__(f"Host key for server '{hostname}' does not match")
        self.hostname, self.key, self.expected_key = hostname, got_key, expected_key


class MissingHostKeyPolicy: pass
class AutoAddPolicy(MissingHostKeyPolicy): pass


class HostKeys:
    def __init__(self): self.entries = []  # (hostname, key type, base64 key)
    def load(self, path):
        with open(path) as f:
            self.entries = [tuple(l.split()[:3]) for l in f if l.strip() and not l.startswith("#")]
    def save(self, path):
        with open(path, "w") as f:
            f.writelines(" ".join(e) + "\n" for e in self.entries)
    def add(self, hostname, keytype, key):
        self.entries = [e for e in self.entries if not (e[0] == hostname and e[1] == keytype)] + [(hostname, keytype, key.get_base64())]
    def lookup(self, hostname):
        found = {e[1]: e[2] for e in self.entries if e[0] == hostname}
        return found or None


class SSHClient:
    def __init__(self): self._host_keys = HostKeys()
    def load_host_keys(self, path): self._host_keys.load(path)
    def set_missing_host_key_policy(self, policy): self._policy = policy


class FakeKey:  # what a device would present: a type and some key material
    def __init__(self, name, material): self.name, self.b64 = name, base64.b64encode(material.encode()).decode()
    def get_name(self): return self.name
    def get_base64(self): return self.b64
