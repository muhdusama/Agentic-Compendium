"""Small server-side Hermes dashboard reader. No cron schedule mutations."""

from __future__ import annotations

import http.cookiejar
import json
import re
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener


class HermesError(RuntimeError):
    """Sanitised error suitable for owner-facing health state."""
    def __init__(self, message="Hermes request failed", category="unavailable"):
        super().__init__(message)
        self.category = category


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_hermes_origin(value, allowed):
    if not isinstance(value, str) or len(value) > 250:
        raise ValueError("invalid Hermes URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Hermes requires an HTTPS server root URL")
    origin = value.rstrip("/")
    if origin not in allowed:
        raise ValueError("Hermes server is not in APP_HERMES_ALLOWED_ORIGINS")
    return origin


def datetime_iso(timestamp):
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HermesAdapter:
    def __init__(self, origin, username, password, timeout=12):
        self.origin, self.username, self.password = origin, username, password
        self.timeout = timeout
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.authed = False

    def request(self, path, params=None, body=None):
        if not self.authed:
            self.login()
        return self._request(path, params, body)

    def _request(self, path, params=None, body=None, expect_json=True):
        url = self.origin + path + ("?" + urlencode(params) if params else "")
        content = None if body is None else json.dumps(body).encode()
        req = Request(url, data=content, method="POST" if body is not None else "GET",
                      headers={"Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                if response.url != url:
                    raise HermesError("Hermes unexpectedly redirected a request")
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise HermesError("Hermes response exceeds size limit")
                try:
                    return json.loads(raw) if expect_json else None
                except json.JSONDecodeError as exc:
                    raise HermesError("Hermes response was malformed", "malformed") from exc
        except HTTPError as exc:
            # Never include response bodies, cookies, usernames or passwords.
            if exc.code in (401, 403):
                raise HermesError("Hermes authentication failed", "auth") from exc
            if exc.code == 429:
                raise HermesError("Hermes rate limited the request", "rate_limit") from exc
            raise HermesError("Hermes service was unavailable", "unavailable") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise HermesError("Hermes request timed out", "timeout") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise HermesError("Hermes request timed out", "timeout") from exc
            raise HermesError("Hermes request failed", "unavailable") from exc
        except ValueError as exc:
            raise HermesError("Hermes request failed", "unavailable") from exc

    def login(self):
        self._request("/auth/password-login", body={
            "provider": "basic", "username": self.username, "password": self.password
        }, expect_json=False)
        self.authed = True

    def jobs(self):
        result = self.request("/api/cron/jobs", {"profile": "all"})
        jobs = result if isinstance(result, list) else result.get("jobs") if isinstance(result, dict) else None
        if not isinstance(jobs, list):
            raise HermesError("Hermes job list was malformed", "malformed")
        return jobs

    def runs(self, job_id, profile):
        params = {"limit": 100}
        if profile:
            params["profile"] = profile
        result = self.request("/api/cron/jobs/" + quote(job_id, safe="") + "/runs", params)
        if not isinstance(result, dict) or not isinstance(result.get("runs"), list):
            raise HermesError("Hermes run list was malformed", "malformed")
        return result["runs"]

    def last_message(self, session_id, profile):
        params = {"profile": profile} if profile else None
        data = self.request("/api/sessions/" + quote(session_id, safe="") + "/messages", params)
        if not isinstance(data, dict) or ("messages" not in data and "data" not in data):
            raise HermesError("Hermes message response was malformed", "malformed")
        messages = data.get("messages", data.get("data"))
        if not isinstance(messages, list):
            raise HermesError("Hermes message response was malformed", "malformed")
        for message in reversed(messages):
            if (isinstance(message, dict) and message.get("role") == "assistant"
                    and message.get("display_kind") != "hidden"
                    and isinstance(message.get("content"), str) and message["content"].strip()):
                return message
        return None

    def pull(self, store, source, enabled, probe_positions=None):
        probe_positions = probe_positions if probe_positions is not None else {}
        imported, deferred, saturated = 0, 0, 0
        for job in self.jobs():
            if not isinstance(job, dict) or job.get("id") not in enabled:
                continue
            job_id = job["id"]
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,150}", job_id):
                continue
            runs = self.runs(job_id, job.get("profile"))
            if len(runs) >= 100:
                saturated += 1
            window = runs[:100]
            progress = probe_positions.get(job_id)
            if not isinstance(progress, dict):
                progress = {"lastProbed": progress, "seen": []} if isinstance(progress, str) else {}
            seen = set(progress.get("seen", []))
            new_runs = [run for run in window if isinstance(run, dict) and run.get("id") not in seen]
            existing_runs = [run for run in window if not isinstance(run, dict) or run.get("id") in seen]
            position = progress.get("lastProbed")
            if position:
                for index, run in enumerate(existing_runs):
                    if isinstance(run, dict) and run.get("id") == position:
                        existing_runs = existing_runs[index + 1:] + existing_runs[:index + 1]
                        break
            window = new_runs + existing_runs
            probes = 0
            # Missing results remain retryable but consume at most 20 message
            # fetches per poll, while the rotating window reaches later runs.
            for run in window:
                if not isinstance(run, dict) or run.get("ended_at") is None:
                    continue
                run_id = run.get("id")
                if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,150}", run_id):
                    continue
                delivery_id = f"hermes-direct:{job_id}:{run_id}"
                if store.find(source, "delivery", delivery_id) or store.find(source, "hidden-delivery", delivery_id):
                    continue
                if run.get("source") != "cron_output" and probes >= 20:
                    deferred += 1
                    continue
                stamp = run.get("started_at")
                if run.get("source") == "cron_output":
                    preview = run.get("preview")
                    if not isinstance(preview, str) or not preview.strip():
                        continue
                    body = "Script-only cron output preview. Full output is not exposed by this Hermes API.\n\n" + preview
                else:
                    probes += 1
                    progress["lastProbed"] = run_id
                    message = self.last_message(run_id, run.get("profile") or job.get("profile"))
                    if not message:
                        continue
                    body = message["content"]
                    stamp = message.get("timestamp") or stamp
                value = {"id": delivery_id, "title": str(job.get("name", job_id)),
                         "publisher": "Hermes", "section": "Inbox", "summary": body[:220],
                         "systemImage": "doc.text", "body": body, "deliveredAt": datetime_iso(stamp),
                         "needsAttention": False, "hermesJobID": job_id}
                result = store.put({"sourceId": source, "kind": "delivery", "key": delivery_id,
                                    "baseVersion": 0, "deleted": False, "value": value})
                if not result.get("conflict"):
                    imported += 1
            # This is only a fair-scheduling cursor. It is deliberately
            # in-memory: no run is marked handled until a delivery is stored.
            progress["seen"] = [run.get("id") for run in runs[:100]
                                if isinstance(run, dict) and isinstance(run.get("id"), str)]
            probe_positions[job_id] = progress
        return {"imported": imported, "deferred": deferred, "jobsAtWindowLimit": saturated,
                "enabledJobs": len(enabled)}
