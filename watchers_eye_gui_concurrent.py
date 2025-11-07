
# watchers_eye_gui_concurrent.py
# Modified PoE Watcher's Eye analyzer
# - Persistent proxy sessions
# - One async worker per proxy
# - Each proxy sends a request every 8 seconds (stable loop)
# - Uses a shared asyncio.Queue of mod combos so proxies take next available job
# - Proxies that die are retried after cooldown (60s)
# UI structure left unchanged

import sys
import asyncio
import sqlite3
import aiohttp
import time
import uuid
import json
import logging
import random
from logging.handlers import RotatingFileHandler
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, QTextEdit, QHBoxLayout,
    QComboBox, QSpinBox, QFormLayout, QCheckBox, QInputDialog, QFileDialog
)
from PyQt5.QtCore import QThread, pyqtSignal, QObject, Qt
from PyQt5.QtGui import QColor
from mod_data import MOD_COMBOS, MOD_NAMES
from app_config import load_settings, save_settings
from watchers_eye_db import ensure_database, export_batch_to_csv

# Configure application logging (rotating file with structured JSON messages)
LOG_PATH = "watcher_debug.log"
_logger = logging.getLogger("watchers_eye")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)

DIVINE_TO_CHAOS_RATE = 100.0
SINGLE_MODS = list(MOD_NAMES.keys())
NAME_TO_MOD_ID = {name: mod_id for mod_id, name in MOD_NAMES.items()}
DATABASE_PATH = Path("data/watchers_eye.db")
EXPORT_DIR = Path("exports")
UPLOAD_ERROR_LOG = Path("watcher_upload_errors.log")
DEFAULT_PROXY_FILES = [
    Path("Webshare 10 proxies.txt"),
    Path("Oxylabs proxies.txt"),
]

class PriceWorker(QObject):
    result_ready = pyqtSignal(float, float, str, str, int)
    status_update = pyqtSignal(str)
    debug_message = pyqtSignal(str)
    proxy_status_update = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.paused = False
        self.single_mode = False
        self.results = []
        self.league = "Keepers"
        self.listing_count = 5

        # proxies
        self.use_proxies = False
        self.proxies = []
        self.proxy_files = [str(p) for p in DEFAULT_PROXY_FILES]
        self.proxy_cooldown = 60  # seconds to wait before retrying dead proxy
        self.proxy_retry_delay = 60
        self.proxy_loop_period = 12  # each proxy must perform request every 8 seconds

        # internal
        self.proxy_sessions = {}  # idx -> aiohttp session or None
        self.proxy_status = {}    # idx -> dict(status='ok'|'dead', dead_since=ts)
        self.queue = None
        self._proxy_tasks = []
        self.loop = None

        # concurrency controls
        self.max_direct_workers = 4
        self._worker_count = 0
        self._stop_tokens_sent = False

        # persistence helpers
        self.completed_keys = set()
        self.divine_to_chaos_rate = DIVINE_TO_CHAOS_RATE

        # logging
        self.debug_log_path = "watcher_debug.log"
        self.proxy_states = {}

        # database
        self.db_path = DATABASE_PATH
        self.batch_id = None
        self.batch_started_at = None
        self._db_conn = None
        self._batch_finalized = False
        self._batch_completed = False
        self.db_queue = None  # asyncio.Queue for DB jobs
        self._db_writer_task = None
        self.export_dir = EXPORT_DIR
        self.upload_error_log = UPLOAD_ERROR_LOG
        self.single_price_lookup = {}
        self.latest_single_batch_id = None
        self.resuming_existing_batch = False
        self.auto_upload = False

    def prepare_for_run(self):
        """Load previously saved results and cache completed modifier keys."""
        self.completed_keys = set()
        self._stop_tokens_sent = False
        self._worker_count = 0
        self.proxy_states = {}
        ensure_database(self.db_path)
        self.batch_id = self._generate_batch_id()
        self.batch_started_at = datetime.utcnow().isoformat()
        self._batch_finalized = False
        self._batch_completed = False
        if not self.single_mode:
            self._load_latest_single_prices()
        else:
            self.single_price_lookup = {}
            self.latest_single_batch_id = None
        display_results = []
        resume_info = self._get_unfinished_batch_info()

        if resume_info:
            self.resuming_existing_batch = True
            self.batch_id = resume_info["batch_id"]
            self.batch_started_at = resume_info["started_at"]
            self.league = resume_info["league"]
            self.divine_to_chaos_rate = resume_info["divine_rate"]
            self.results = self._load_batch_results(self.batch_id, use_mode=self.single_mode)
            display_results = list(self.results)
            for entry in self.results:
                key = self._extract_key_from_entry(entry)
                if key:
                    self.completed_keys.add(key)
            self._emit_debug(f"Resuming unfinished batch {self.batch_id}.")
        else:
            self.batch_id = self._generate_batch_id()
            self.batch_started_at = datetime.utcnow().isoformat()
            self.results = []
            # Do not preload completed batch results into the UI; start fresh
            display_results = []
            self._emit_debug(f"Starting new batch {self.batch_id} for league={self.league}.")
            self.resuming_existing_batch = False

        self._emit_debug(
            f"Prepared run with {len(self.completed_keys)} previously completed modifiers "
            f"(single_mode={self.single_mode}, rate={self.divine_to_chaos_rate})."
        )
        return list(display_results)

    def _emit_debug(self, message):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        # Emit to GUI panel (human friendly)
        self.debug_message.emit(f"[{ts}] {message}")
        # Emit structured JSON to rotating log file
        try:
            payload = {
                "ts": ts,
                "level": "INFO",
                "batch_id": getattr(self, "batch_id", None),
                "league": getattr(self, "league", None),
                "single_mode": bool(getattr(self, "single_mode", False)),
                "msg": message,
            }
            _logger.info(json.dumps(payload, ensure_ascii=False))
        except Exception:
            # Never raise from logging
            pass

    def _set_proxy_state(self, idx, state, detail=None):
        if idx is None or idx < 0:
            return
        previous = self.proxy_states.get(idx)
        state_changed = previous != state
        if state_changed:
            self.proxy_states[idx] = state
            self.proxy_status_update.emit(idx, state)
        if detail:
            self._emit_debug(detail)
        elif state_changed:
            self._emit_debug(f"Proxy {idx} state -> {state}")
        # Structured event for proxy state changes
        try:
            payload = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "level": "INFO",
                "batch_id": getattr(self, "batch_id", None),
                "league": getattr(self, "league", None),
                "single_mode": bool(getattr(self, "single_mode", False)),
                "event": "proxy_state",
                "proxy_idx": idx,
                "state": state,
            }
            _logger.info(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _handle_rate_limit(self, proxy_idx, job_desc, wait_time):
        message = f"Rate limit hit while fetching {job_desc}. Cooling down for {wait_time}s."
        if proxy_idx is not None and proxy_idx >= 0:
            self._set_proxy_state(
                proxy_idx,
                "rate_limited",
                detail=f"Proxy {proxy_idx} hit rate limit while fetching {job_desc}. Cooling down for {wait_time}s."
            )
        else:
            self._emit_debug(f"[DIRECT] {message}")
        # Structured event for rate limit
        try:
            payload = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "level": "INFO",
                "batch_id": getattr(self, "batch_id", None),
                "league": getattr(self, "league", None),
                "single_mode": bool(getattr(self, "single_mode", False)),
                "event": "rate_limit",
                "proxy_idx": proxy_idx,
                "wait_time": wait_time,
                "job": job_desc,
            }
            _logger.info(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _resolve_mod_id(self, value):
        if not value:
            return None
        if value in MOD_NAMES:
            return value
        return NAME_TO_MOD_ID.get(value)

    def _extract_key_from_entry(self, entry):
        if not isinstance(entry, dict):
            return None
        mod1 = entry.get("mod1_id") or self._resolve_mod_id(entry.get("mod1"))
        if self.single_mode:
            if not mod1:
                return None
            return (mod1,)
        mod2 = entry.get("mod2_id") or self._resolve_mod_id(entry.get("mod2"))
        if mod1 and mod2:
            return (mod1, mod2)
        return None

    def _make_key(self, mod1, mod2=None):
        if self.single_mode:
            return (mod1,)
        return (mod1, mod2)

    def _generate_batch_id(self):
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        mode = "single" if self.single_mode else "double"
        suffix = uuid.uuid4().hex[:4]
        return f"{timestamp}-{mode}-{suffix}"

    async def _inject_stop_tokens(self):
        if not self.queue:
            return
        tokens = max(1, self._worker_count or 0)
        for _ in range(tokens):
            await self.queue.put(None)

    def _open_db_connection(self):
        if self._db_conn:
            return
        try:
            # allow usage from a worker thread via run_in_executor
            self._db_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._db_conn.execute("PRAGMA foreign_keys = ON")
            self._emit_debug(f"Opened database connection at {self.db_path}.")
        except Exception as exc:
            self._emit_debug(f"Database connection error: {exc}")
            self._db_conn = None

    def _close_db_connection(self):
        if not self._db_conn:
            return
        try:
            self._db_conn.close()
            self._emit_debug("Database connection closed.")
        except Exception as exc:
            self._emit_debug(f"Error closing database connection: {exc}")
        finally:
            self._db_conn = None

    def _create_batch_record(self):
        if not self._db_conn or not self.batch_id:
            return
        if getattr(self, "resuming_existing_batch", False):
            self._emit_debug(f"Using existing batch record {self.batch_id}.")
            return
        run_mode = "single" if self.single_mode else "double"
        started_at = self.batch_started_at or datetime.utcnow().isoformat()
        try:
            self._db_conn.execute(
                """
                INSERT OR REPLACE INTO batches (batch_id, run_mode, league, divine_rate, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.batch_id, run_mode, self.league, self.divine_to_chaos_rate, started_at),
            )
            self._db_conn.commit()
            self._emit_debug(f"Batch {self.batch_id} recorded in database.")
        except Exception as exc:
            self._emit_debug(f"Failed to record batch {self.batch_id}: {exc}")

    def _finalize_batch_record(self):
        if self._batch_finalized or not self._db_conn or not self.batch_id:
            return
        finished_at = datetime.utcnow().isoformat()
        try:
            self._db_conn.execute(
                "UPDATE batches SET finished_at = ? WHERE batch_id = ?",
                (finished_at, self.batch_id),
            )
            self._db_conn.commit()
            self._batch_finalized = True
            self._emit_debug(f"Batch {self.batch_id} finalized at {finished_at}.")
        except Exception as exc:
            self._emit_debug(f"Failed to finalize batch {self.batch_id}: {exc}")

    async def _start_db_writer(self):
        if self.db_queue is None:
            self.db_queue = asyncio.Queue()
        if not self._db_writer_task:
            self._db_writer_task = asyncio.create_task(self._db_writer())

    async def _stop_db_writer(self):
        if self.db_queue is not None:
            await self.db_queue.join()
        if self._db_writer_task:
            self._db_writer_task.cancel()
            try:
                await self._db_writer_task
            except Exception:
                pass
            self._db_writer_task = None

    async def _db_writer(self):
        # Single consumer for DB jobs; executes blocking writes in a worker thread
        while True:
            job = await self.db_queue.get()
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._execute_db_job, job)
            finally:
                self.db_queue.task_done()

    def _execute_db_job(self, job):
        if not self._db_conn:
            return
        kind = job.get("kind")
        if kind == "single":
            try:
                self._db_conn.execute(
                    """
                    INSERT INTO prices_single (
                        batch_id, mod_id, mod_name, avg_divine, avg_chaos,
                        divine_to_chaos_rate, listing_count, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["batch_id"],
                        job["mod_id"],
                        job["mod_name"],
                        float(job["avg_div"]),
                        float(job["avg_chaos"]),
                        float(job["rate"]),
                        int(job["listings"]),
                        job["recorded_at"],
                    ),
                )
                self._db_conn.commit()
            except Exception as exc:
                self._emit_debug(f"DB writer failed (single {job.get('mod_id')}): {exc}")
        elif kind == "double":
            try:
                self._db_conn.execute(
                    """
                    INSERT INTO prices_double (
                        batch_id, mod1_id, mod2_id, mod1_name, mod2_name,
                        avg_divine, avg_chaos, divine_to_chaos_rate,
                        listing_count, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["batch_id"],
                        job["mod1_id"],
                        job["mod2_id"],
                        job["mod1_name"],
                        job["mod2_name"],
                        float(job["avg_div"]),
                        float(job["avg_chaos"]),
                        float(job["rate"]),
                        int(job["listings"]),
                        job["recorded_at"],
                    ),
                )
                self._db_conn.commit()
            except Exception as exc:
                self._emit_debug(f"DB writer failed (double {job.get('mod1_id')}/{job.get('mod2_id')}): {exc}")

    def _export_batch_results(self):
        if not self.batch_id:
            return None
        mode = "single" if self.single_mode else "double"
        self.export_dir.mkdir(parents=True, exist_ok=True)
        export_path = self.export_dir / f"{self.batch_id}_{mode}.csv"
        try:
            export_batch_to_csv(self.db_path, self.batch_id, export_path, mode)
            self._emit_debug(f"Exported batch {self.batch_id} to {export_path}.")
            return export_path
        except Exception as exc:
            self._emit_debug(f"Failed to export batch {self.batch_id}: {exc}")
            return None

    def _sync_to_google_sheets(self, csv_path):
        mode = "single" if self.single_mode else "double"
        try:
            from google_drive_sync import (
                GoogleSheetsConfigError,
                upload_csv_to_google_sheet,
            )
        except ImportError as exc:
            message = f"Google sync skipped (missing dependency): {exc}"
            self._emit_debug(message)
            self._write_upload_error(message)
            return

        try:
            upload_csv_to_google_sheet(csv_path, mode)
            self._emit_debug(f"Uploaded {csv_path} to Google Sheets ({mode} sheet).")
        except GoogleSheetsConfigError as exc:
            message = f"Google Sheets configuration error: {exc}"
            self._emit_debug(message)
            self._write_upload_error(message)
        except Exception as exc:
            message = f"Google Sheets upload failed: {exc}"
            self._emit_debug(message)
            self._write_upload_error(message)

    def _write_upload_error(self, message):
        try:
            timestamp = datetime.utcnow().isoformat()
            with open(self.upload_error_log, "a", encoding="utf-8") as log_file:
                log_file.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

    def _load_latest_single_prices(self):
        self.single_price_lookup = {}
        self.latest_single_batch_id = None
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT batch_id
                    FROM batches
                    WHERE run_mode = 'single' AND finished_at IS NOT NULL
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                if not row:
                    self._emit_debug("No completed single batches available for fallback pricing.")
                    return
                batch_id = row[0]
                data = conn.execute(
                    """
                    SELECT mod_id, avg_divine, avg_chaos
                    FROM prices_single
                    WHERE batch_id = ?
                    ORDER BY recorded_at DESC
                    """,
                    (batch_id,),
                ).fetchall()
        except Exception as exc:
            self._emit_debug(f"Failed to load fallback single prices: {exc}")
            return

        for mod_id, avg_div, avg_chaos in data:
            if mod_id not in self.single_price_lookup:
                self.single_price_lookup[mod_id] = (float(avg_div), float(avg_chaos))
        self.latest_single_batch_id = batch_id
        self._emit_debug(
            f"Loaded {len(self.single_price_lookup)} fallback single prices from batch {batch_id}."
        )

    def _get_unfinished_batch_info(self):
        return self._fetch_batch_info(unfinished_only=True)

    def _get_latest_batch_info(self):
        return self._fetch_batch_info(unfinished_only=False)

    def _fetch_batch_info(self, unfinished_only):
        mode = "single" if self.single_mode else "double"
        query = """
            SELECT batch_id, league, divine_rate, started_at, finished_at
            FROM batches
            WHERE run_mode = ?
        """
        params = [mode]
        if unfinished_only is True:
            query += " AND finished_at IS NULL"
        query += " ORDER BY started_at DESC LIMIT 1"
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(query, params).fetchone()
        except Exception as exc:
            self._emit_debug(f"Failed to fetch batch info: {exc}")
            return None
        if not row:
            return None
        return {
            "batch_id": row[0],
            "league": row[1],
            "divine_rate": row[2],
            "started_at": row[3],
            "finished_at": row[4],
        }

    def _load_batch_results(self, batch_id, use_mode=None):
        if not batch_id:
            return []
        mode = self.single_mode if use_mode is None else use_mode
        try:
            with sqlite3.connect(self.db_path) as conn:
                rate_row = conn.execute(
                    "SELECT divine_rate FROM batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                batch_rate = float(rate_row[0]) if rate_row and rate_row[0] is not None else self.divine_to_chaos_rate
                if mode:
                    rows = conn.execute(
                        """
                        SELECT mod_id, mod_name, avg_divine, avg_chaos, recorded_at
                        FROM prices_single
                        WHERE batch_id = ?
                        ORDER BY recorded_at ASC
                        """,
                        (batch_id,),
                    ).fetchall()
                    return [
                        {
                            "mod1_id": mod_id,
                            "mod1_name": mod_name,
                            "mod2_id": None,
                            "mod2_name": "-",
                            "avg_price_divine": float(avg_div),
                            "avg_price_chaos": float(avg_chaos),
                            "divine_to_chaos_rate": batch_rate,
                            "recorded_at": recorded_at,
                            "batch_id": batch_id,
                            "proxy_index": -1,
                        }
                        for mod_id, mod_name, avg_div, avg_chaos, recorded_at in rows
                    ]
                else:
                    rows = conn.execute(
                        """
                        SELECT mod1_id, mod2_id, mod1_name, mod2_name,
                               avg_divine, avg_chaos, recorded_at
                        FROM prices_double
                        WHERE batch_id = ?
                        ORDER BY recorded_at ASC
                        """,
                        (batch_id,),
                    ).fetchall()
                    return [
                        {
                            "mod1_id": mod1,
                            "mod1_name": mod1_name,
                            "mod2_id": mod2,
                            "mod2_name": mod2_name,
                            "avg_price_divine": float(avg_div),
                            "avg_price_chaos": float(avg_chaos),
                            "divine_to_chaos_rate": batch_rate,
                            "recorded_at": recorded_at,
                            "batch_id": batch_id,
                            "proxy_index": -1,
                        }
                        for mod1, mod2, mod1_name, mod2_name, avg_div, avg_chaos, recorded_at in rows
                    ]
        except Exception as exc:
            self._emit_debug(f"Failed to load batch results for {batch_id}: {exc}")
            return []

    def _get_latest_single_price(self, mod_id):
        if self.single_price_lookup:
            price = self.single_price_lookup.get(mod_id)
            if price:
                return price
        if not self._db_conn:
            return None
        try:
            row = self._db_conn.execute(
                """
                SELECT avg_divine, avg_chaos
                FROM prices_single
                WHERE mod_id = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (mod_id,),
            ).fetchone()
        except Exception as exc:
            self._emit_debug(f"Failed to load single price for {mod_id}: {exc}")
            return None
        if not row:
            return None
        return float(row[0]), float(row[1])

    def _fallback_double_price(self, mod1, mod2):
        price1 = self._get_latest_single_price(mod1)
        price2 = self._get_latest_single_price(mod2)
        candidates = []
        if price1:
            candidates.append(("mod1", *price1))
        if price2:
            candidates.append(("mod2", *price2))
        if not candidates:
            self._emit_debug(
                f"No fallback single prices available for "
                f"{MOD_NAMES.get(mod1)} + {MOD_NAMES.get(mod2)}"
            )
            return None
        best = max(candidates, key=lambda item: (item[1], item[2]))
        source, divine, chaos = best
        self._emit_debug(
            f"Using fallback ({source}) single price for "
            f"{MOD_NAMES.get(mod1)} + {MOD_NAMES.get(mod2)}: {divine:.2f} div."
        )
        return float(divine), float(chaos)

    def load_proxies(self):
        proxy_list = []
        seen = set()
        for file_path in (self.proxy_files or []):
            entries = self._parse_proxy_file(file_path)
            for proxy in entries:
                if proxy not in seen:
                    proxy_list.append(proxy)
                    seen.add(proxy)
        self.proxies = proxy_list
        self._emit_debug(f"Loaded {len(self.proxies)} proxies from configured lists.")
        return len(self.proxies) > 0

    def _parse_proxy_file(self, file_path):
        path = Path(file_path)
        if not path.exists():
            return []
        parsed = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split(":")
                    proxy_url = None
                    if len(parts) == 4:
                        host, port, username, password = parts
                        proxy_url = f"http://{username}:{password}@{host}:{port}"
                    elif len(parts) == 2:
                        host, port = parts
                        proxy_url = f"http://{host}:{port}"
                    else:
                        self._emit_debug(f"Skipping unrecognized proxy format in {path}: {line}")
                        continue
                    parsed.append(proxy_url)
            self._emit_debug(f"Loaded {len(parsed)} proxies from {path}.")
        except Exception as exc:
            self._emit_debug(f"Failed to load proxies from {path}: {exc}")
        return parsed

    async def _ensure_proxy_session(self, idx):
        """Create session for proxy idx if missing or closed."""
        if idx in self.proxy_sessions:
            sess = self.proxy_sessions[idx]
            if sess and not sess.closed:
                return sess
        # create session
        headers = {"User-Agent": "poe-watchers-eye-analyzer/1.0", "Content-Type": "application/json"}
        conn = aiohttp.TCPConnector(limit=20, limit_per_host=10, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=30)
        session = aiohttp.ClientSession(headers=headers, connector=conn, timeout=timeout)
        self.proxy_sessions[idx] = session
        self.proxy_status[idx] = {"status": "ok", "dead_since": None}
        if idx < len(self.proxies):
            masked_proxy = self.proxies[idx]
            if "@" in masked_proxy:
                masked_proxy = masked_proxy.split("@", 1)[1]
        else:
            masked_proxy = "?"
        self._emit_debug(f"Created persistent session for proxy {idx} ({masked_proxy})")
        self._set_proxy_state(idx, "ok")
        return session

    async def _close_proxy_session(self, idx):
        sess = self.proxy_sessions.get(idx)
        if sess:
            try:
                await sess.close()
            except Exception as e:
                self._emit_debug(f"Error closing session {idx}: {e}")
        self.proxy_sessions.pop(idx, None)
        self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
        self._set_proxy_state(idx, "dead", detail=f"Proxy {idx} session closed and marked dead.")

    async def _fill_queue(self):
        """Populate the queue with mod combos. We'll cycle them by re-adding after processing."""
        await asyncio.sleep(0)  # allow event loop scheduling
        mod_list = SINGLE_MODS if self.single_mode else MOD_COMBOS
        # put all items initially
        enqueued = 0
        for m in mod_list:
            if self.single_mode:
                key = self._make_key(m)
            else:
                mod1, mod2 = m
                key = self._make_key(mod1, mod2)
            if key in self.completed_keys:
                continue
            await self.queue.put(m)
            enqueued += 1
        if enqueued:
            self._emit_debug(f"Queue populated with {enqueued} items")
        else:
            self._emit_debug("Queue is empty; nothing pending to fetch")
        return enqueued

    async def _proxy_worker(self, idx):
        """Worker that runs perpetually for a single proxy.
        Takes next mod combo from queue, performs request, then sleeps so the interval between requests is >= period."""
        proxy_url = self.proxies[idx] if idx < len(self.proxies) else None
        self._emit_debug(f"Proxy worker {idx} starting. Loop period={self.proxy_loop_period}s")
        # randomize initial offset to avoid synchronized bursts
        await asyncio.sleep(random.uniform(0, 0.8))
        while self.running:
            if self.paused:
                await asyncio.sleep(0.5)
                continue

            # ensure session exists (lazy create)
            try:
                if idx not in self.proxy_sessions or self.proxy_sessions.get(idx) is None or self.proxy_sessions[idx].closed:
                    await self._ensure_proxy_session(idx)
                session = self.proxy_sessions[idx]
            except Exception as e:
                self._emit_debug(f"Proxy {idx} session create failed: {e}")
                self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
                self._set_proxy_state(idx, "error", detail=f"Proxy {idx} failed to establish session: {e}")
                await asyncio.sleep(self.proxy_retry_delay)
                continue

            # skip if marked dead and not cooled down
            status = self.proxy_status.get(idx, {"status":"ok","dead_since":None})
            if status["status"] == "dead":
                dead_since = status.get("dead_since") or 0
                if time.time() - dead_since < self.proxy_cooldown:
                    self._set_proxy_state(idx, "cooldown")
                    await asyncio.sleep(1)
                    continue
                else:
                    # retry
                    self._emit_debug(f"Retrying proxy {idx} after cooldown")
                    try:
                        await self._close_proxy_session(idx)
                    except Exception:
                        pass
                    try:
                        await self._ensure_proxy_session(idx)
                        status = {"status":"ok","dead_since":None}
                        self.proxy_status[idx] = status
                        self._set_proxy_state(idx, "ok", detail=f"Proxy {idx} re-established after cooldown.")
                    except Exception as e:
                        self._emit_debug(f"Proxy {idx} still failing on recreate: {e}")
                        self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
                        self._set_proxy_state(idx, "error", detail=f"Proxy {idx} recreate failure: {e}")
                        await asyncio.sleep(self.proxy_retry_delay)
                        continue

            # take a job
            item = None
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                # nothing to do, loop sleep
                await asyncio.sleep(1)
                continue

            if item is None:
                self.queue.task_done()
                self._emit_debug(f"Proxy worker {idx} received stop token.")
                break

            start_ts = time.time()
            to_sleep = 0
            job_desc = ""
            retry_item = False
            try:
                if self.single_mode:
                    mod1 = item
                    avg_div, avg_chaos = await self._query_price_single_impl(session, mod1, proxy_url, proxy_idx=idx)
                    m1_name = MOD_NAMES.get(mod1, str(mod1))
                    m2_name = "-"
                    job_desc = m1_name
                else:
                    mod1, mod2 = item
                    avg_div, avg_chaos = await self._query_price_impl(session, mod1, mod2, proxy_url, proxy_idx=idx)
                    m1_name = MOD_NAMES.get(mod1, str(mod1))
                    m2_name = MOD_NAMES.get(mod2, str(mod2))
                    job_desc = f"{m1_name} + {m2_name}"
                if avg_div is not None:
                    self._emit_debug(f"Proxy {idx} completed fetch for {job_desc} with result: {avg_div:.2f} div.")
                    self.result_ready.emit(avg_div, avg_chaos, m1_name, m2_name, idx)
                    self.add_result(mod1, mod2 if not self.single_mode else None, avg_div, avg_chaos, idx)
                    # Structured result log
                    try:
                        payload = {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                            "level": "INFO",
                            "batch_id": getattr(self, "batch_id", None),
                            "league": getattr(self, "league", None),
                            "single_mode": bool(getattr(self, "single_mode", False)),
                            "event": "result",
                            "proxy_idx": idx,
                            "mod1": MOD_NAMES.get(mod1, str(mod1)),
                            "mod2": None if self.single_mode else MOD_NAMES.get(mod2, str(mod2)),
                            "avg_div": float(avg_div),
                            "avg_chaos": float(avg_chaos) if avg_chaos is not None else None,
                            "elapsed": round(time.time() - start_ts, 3),
                        }
                        _logger.info(json.dumps(payload, ensure_ascii=False))
                    except Exception:
                        pass
                    self._set_proxy_state(idx, "ok")
                else:
                    self._emit_debug(f"Proxy {idx} returned no price for {job_desc}")
            except Exception as e:
                self._emit_debug(f"Proxy {idx} task error for {job_desc or item}: {e}")
                # mark dead and close session
                self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
                self._set_proxy_state(idx, "error", detail=f"Proxy {idx} encountered error: {e}")
                try:
                    await self._close_proxy_session(idx)
                except Exception:
                    pass
                retry_item = True
            finally:
                self.queue.task_done()
                if retry_item and self.running and item is not None:
                    await self.queue.put(item)
                    self._emit_debug(f"Requeued job {job_desc or item} after proxy {idx} error.")
                # ensure at least proxy_loop_period seconds between requests per proxy
                elapsed = time.time() - start_ts
                # add slight jitter to avoid alignment
                to_sleep = max(0, self.proxy_loop_period - elapsed) + random.uniform(0, 0.5)

            if not self.running:
                break
            await asyncio.sleep(to_sleep)

    async def _direct_worker(self):
        """Fallback worker when not using proxies. Multiple workers will process queue with a small sleep between tasks."""
        # create a shared session for direct workers
        headers = {"User-Agent": "poe-watchers-eye-analyzer/1.0", "Content-Type": "application/json"}
        conn = aiohttp.TCPConnector(limit=20, limit_per_host=10, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=30)
        session = aiohttp.ClientSession(headers=headers, connector=conn, timeout=timeout)
        try:
            while self.running:
                if self.paused:
                    await asyncio.sleep(0.5)
                    continue
                item = None
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.5)
                    continue
                if item is None:
                    self.queue.task_done()
                    break
                start_ts = time.time()
                to_sleep = 0
                job_desc = ""
                retry_item = False
                try:
                    if self.single_mode:
                        mod1 = item
                        avg_div, avg_chaos = await self._query_price_single_impl(session, mod1, proxy_url=None, proxy_idx=None)
                        m1_name = MOD_NAMES.get(mod1, str(mod1))
                        m2_name = "-"
                        job_desc = m1_name
                    else:
                        mod1, mod2 = item
                        avg_div, avg_chaos = await self._query_price_impl(session, mod1, mod2, proxy_url=None, proxy_idx=None)
                        m1_name = MOD_NAMES.get(mod1, str(mod1))
                        m2_name = MOD_NAMES.get(mod2, str(mod2))
                        job_desc = f"{m1_name} + {m2_name}"
                    if avg_div is not None:
                        self.result_ready.emit(avg_div, avg_chaos, m1_name, m2_name, -1)
                        self.add_result(mod1, mod2 if not self.single_mode else None, avg_div, avg_chaos, None)
                        # Structured result log (direct)
                        try:
                            payload = {
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                                "level": "INFO",
                                "batch_id": getattr(self, "batch_id", None),
                                "league": getattr(self, "league", None),
                                "single_mode": bool(getattr(self, "single_mode", False)),
                                "event": "result",
                                "proxy_idx": None,
                                "mod1": MOD_NAMES.get(mod1, str(mod1)),
                                "mod2": None if self.single_mode else MOD_NAMES.get(mod2, str(mod2)),
                                "avg_div": float(avg_div),
                                "avg_chaos": float(avg_chaos) if avg_chaos is not None else None,
                                "elapsed": round(time.time() - start_ts, 3),
                            }
                            _logger.info(json.dumps(payload, ensure_ascii=False))
                        except Exception:
                            pass
                    else:
                        self._emit_debug(f"Direct worker returned no price for {job_desc}")
                except Exception as e:
                    self._emit_debug(f"Direct worker error for {job_desc or item}: {e}")
                    retry_item = True
                finally:
                    self.queue.task_done()
                    if retry_item and self.running and item is not None:
                        await self.queue.put(item)
                        self._emit_debug(f"Requeued job {job_desc or item} after direct worker error.")
                    elapsed = time.time() - start_ts
                    # keep direct workers modestly paced
                    to_sleep = max(0.5, 1.0 - elapsed)
                if not self.running:
                    break
                await asyncio.sleep(to_sleep)
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def start_async(self):
        """Main entry inside event loop. Prepares queue and starts proxy workers or direct workers."""
        self.queue = asyncio.Queue()
        enqueued = await self._fill_queue()

        if enqueued == 0:
            self.status_update.emit("All modifiers already fetched. Nothing to queue.")
            self.running = False
            return

        self._open_db_connection()
        self._create_batch_record()
        await self._start_db_writer()

        # if proxies requested, ensure they are loaded and create workers per proxy
        if self.use_proxies:
            if not self.load_proxies():
                self._emit_debug("No proxies loaded, falling back to direct mode.")
                self.use_proxies = False

        tasks = []
        self._proxy_tasks = []
        if self.use_proxies:
            # create proxy tasks
            for idx in range(len(self.proxies)):
                # initialize status as ok
                self.proxy_status[idx] = {"status":"ok","dead_since":None}
                # do not pre-create session yet; lazy create in worker
                t = asyncio.create_task(self._proxy_worker(idx))
                tasks.append(t)
                self._proxy_tasks.append(t)
            self._emit_debug(f"Started {len(tasks)} proxy workers")
        else:
            # create a pool of direct workers
            for _ in range(self.max_direct_workers):
                t = asyncio.create_task(self._direct_worker())
                tasks.append(t)
            self._emit_debug(f"Started {len(tasks)} direct workers")

        self._worker_count = len(tasks)
        self.status_update.emit(f"Queued {enqueued} modifiers; running {self._worker_count} workers.")
        self._emit_debug(f"Run started for {enqueued} modifiers (single_mode={self.single_mode}, proxies={'on' if self.use_proxies else 'off'}, rate={self.divine_to_chaos_rate}).")

        completion_task = asyncio.create_task(self.queue.join())

        try:
            while self.running:
                if completion_task.done():
                    self._emit_debug("All queued modifiers processed.")
                    self.status_update.emit("Completed.")
                    self.running = False
                    self._batch_completed = True
                    if not self._stop_tokens_sent:
                        self._stop_tokens_sent = True
                        await self._inject_stop_tokens()
                    break
                await asyncio.sleep(0.5)
        finally:
            completion_task.cancel()
            # cancel tasks
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Flush DB writes before finalizing
            try:
                await self._stop_db_writer()
            except Exception:
                pass
            # close any remaining proxy sessions
            for idx in list(self.proxy_sessions.keys()):
                try:
                    await self._close_proxy_session(idx)
                except Exception:
                    pass
            self._emit_debug("Worker stopped and sessions closed")
            if self._batch_completed:
                self._finalize_batch_record()
            else:
                self._emit_debug("Batch incomplete; leaving record unfinished.")
            self._close_db_connection()
            if self._batch_completed:
                export_path = self._export_batch_results()
                if export_path:
                    self.status_update.emit(f"Exported batch data to {export_path}")
                    if self.auto_upload:
                        self._sync_to_google_sheets(export_path)
                    else:
                        self._emit_debug("Auto-upload disabled; skipping Google Sheets sync.")
            else:
                self._emit_debug("Skipped export/upload because batch did not finish.")

    def start(self):
        # create and set event loop for thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.start_async())
        except Exception as e:
            self._emit_debug(f"Event loop error: {e}")
        finally:
            # ensure cleanup
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            try:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            # close loop
            try:
                self.loop.close()
            except Exception:
                pass

    # ---- Query implementations (adapted from previous version) ----
    async def _query_price_impl(self, session, mod1, mod2, proxy_url=None, proxy_idx=None, retry=True):
        if not self.running:
            return None, None
        try:
            mod1_name = MOD_NAMES.get(mod1, str(mod1))
            mod2_name = MOD_NAMES.get(mod2, str(mod2))
            job_desc = f"{mod1_name} + {mod2_name}"
            payload = {
                "query": {
                    "status": {"option": "available"},
                    "stats": [{
                        "type": "and",
                        "filters": [
                            {"id": mod1, "disabled": False},
                            {"id": mod2, "disabled": False}
                        ]
                    }],
                    "filters": {"trade_filters": {"disabled": False}}
                },
                "sort": {"price": "asc"}
            }
            url = f"https://www.pathofexile.com/api/trade/search/{self.league}"
            self._emit_debug(f"[SEARCH] proxy={proxy_idx} {job_desc}")

            try:
                async with session.post(url, json=payload, proxy=proxy_url) as r:
                    self._emit_debug(f"[SEARCH RESP] proxy={proxy_idx} status={r.status} for {job_desc}")
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        self._handle_rate_limit(proxy_idx, job_desc, wait_time)
                        await asyncio.sleep(wait_time)
                        return await self._query_price_impl(session, mod1, mod2, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self._emit_debug(f"Search error {r.status} for {job_desc} (proxy={proxy_idx}).")
                        return None, None
                    search_data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._emit_debug(f"Network error (search) for {job_desc}: {e}")
                raise

            if not search_data.get("result"):
                self._emit_debug(f"No search results for {job_desc}.")
                fallback = self._fallback_double_price(mod1, mod2)
                if fallback:
                    return fallback
                return None, None

            ids = search_data["result"][:self.listing_count]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(ids)}?query={search_data['id']}"
            self._emit_debug(f"[FETCH] proxy={proxy_idx} {fetch_url}")

            try:
                async with session.get(fetch_url, proxy=proxy_url) as r:
                    self._emit_debug(f"[FETCH RESP] proxy={proxy_idx} status={r.status} for {job_desc}")
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        self._handle_rate_limit(proxy_idx, job_desc, wait_time)
                        await asyncio.sleep(wait_time)
                        return await self._query_price_impl(session, mod1, mod2, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self._emit_debug(f"Fetch error {r.status} for {job_desc} (proxy={proxy_idx}).")
                        return None, None
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._emit_debug(f"Network error (fetch) for {job_desc}: {e}")
                raise

            prices_divine = []
            prices_chaos = []
            for item in data.get("result", []):
                listing = item["listing"]["price"]
                amount = listing["amount"]
                currency = listing["currency"]
                if currency == "divine":
                    prices_divine.append(amount)
                    prices_chaos.append(amount * self.divine_to_chaos_rate)
                elif currency == "chaos":
                    prices_chaos.append(amount)
                    prices_divine.append(amount / self.divine_to_chaos_rate)

            if not prices_divine:
                self._emit_debug(f"No prices parsed for {job_desc}.")
                fallback = self._fallback_double_price(mod1, mod2)
                if fallback:
                    return fallback
                return None, None

            avg_divine = sum(prices_divine) / len(prices_divine)
            avg_chaos = sum(prices_chaos) / len(prices_chaos)
            self._emit_debug(f"[AVG] {job_desc}: {avg_divine:.2f} div ({avg_chaos:.1f} chaos)")
            if proxy_idx is not None:
                self._set_proxy_state(proxy_idx, "ok")
            return avg_divine, avg_chaos
        except Exception as e:
            self._emit_debug(f"Query exception for {job_desc}: {e}")
            raise

    async def _query_price_single_impl(self, session, mod1, proxy_url=None, proxy_idx=None, retry=True):
        if not self.running:
            return None, None
        try:
            mod1_name = MOD_NAMES.get(mod1, str(mod1))
            payload = {
                "query": {
                    "status": {"option": "available"},
                    "stats": [{
                        "type": "and",
                        "filters": [{"id": mod1, "disabled": False}]
                    }],
                    "filters": {
                        "misc_filters": {
                            "disabled": False,
                            "filters": {
                                "corrupted": {"option": "false"},
                                "ilvl": {"min": 86}
                            }
                        },
                        "trade_filters": {"disabled": False}}
                },
                "sort": {"price": "asc"}
            }
            url = f"https://www.pathofexile.com/api/trade/search/{self.league}"
            self._emit_debug(f"[SEARCH SINGLE] proxy={proxy_idx} {mod1_name}")

            try:
                async with session.post(url, json=payload, proxy=proxy_url) as r:
                    self._emit_debug(f"[SEARCH SINGLE RESP] proxy={proxy_idx} status={r.status} for {mod1_name}")
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        self._handle_rate_limit(proxy_idx, mod1_name, wait_time)
                        await asyncio.sleep(wait_time)
                        return await self._query_price_single_impl(session, mod1, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self._emit_debug(f"Search error {r.status} for {mod1_name} (proxy={proxy_idx}).")
                        return None, None
                    search_data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._emit_debug(f"Network error (search single) for {mod1_name}: {e}")
                raise

            if not search_data.get("result"):
                self._emit_debug(f"No search results for {mod1_name}.")
                return None, None

            ids = search_data["result"][:self.listing_count]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(ids)}?query={search_data['id']}"

            try:
                async with session.get(fetch_url, proxy=proxy_url) as r:
                    self._emit_debug(f"[FETCH SINGLE RESP] proxy={proxy_idx} status={r.status} for {mod1_name}")
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        self._handle_rate_limit(proxy_idx, mod1_name, wait_time)
                        await asyncio.sleep(wait_time)
                        return await self._query_price_single_impl(session, mod1, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self._emit_debug(f"Fetch error {r.status} for {mod1_name} (proxy={proxy_idx}).")
                        return None, None
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._emit_debug(f"Network error (fetch single) for {mod1_name}: {e}")
                raise

            prices_divine = []
            prices_chaos = []
            for item in data.get("result", []):
                listing = item["listing"]["price"]
                amount = listing["amount"]
                currency = listing["currency"]
                if currency == "divine":
                    prices_divine.append(amount)
                    prices_chaos.append(amount * self.divine_to_chaos_rate)
                elif currency == "chaos":
                    prices_chaos.append(amount)
                    prices_divine.append(amount / self.divine_to_chaos_rate)

            if not prices_divine:
                self._emit_debug(f"No prices parsed for {mod1_name}.")
                return None, None

            avg_divine = sum(prices_divine) / len(prices_divine)
            avg_chaos = sum(prices_chaos) / len(prices_chaos)
            self._emit_debug(f"[AVG SINGLE] {mod1_name}: {avg_divine:.2f} div ({avg_chaos:.1f} chaos)")
            if proxy_idx is not None:
                self._set_proxy_state(proxy_idx, "ok")
            return avg_divine, avg_chaos
        except Exception as e:
            self._emit_debug(f"Query single exception for {mod1_name}: {e}")
            raise

    # ---- Result bookkeeping ----
    def add_result(self, mod1, mod2, avg_price_div, avg_price_chaos, proxy_idx=None):
        recorded_at = datetime.utcnow().isoformat()
        key = self._make_key(mod1, mod2)
        result = {
            "mod1_id": mod1,
            "mod1_name": MOD_NAMES.get(mod1, str(mod1)),
            "mod2_id": mod2 if mod2 else None,
            "mod2_name": MOD_NAMES.get(mod2, str(mod2)) if mod2 else None,
            "avg_price_divine": round(avg_price_div, 2),
            "avg_price_chaos": round(avg_price_chaos, 1),
            "divine_to_chaos_rate": round(self.divine_to_chaos_rate, 2),
            "batch_id": self.batch_id,
            "recorded_at": recorded_at
        }
        if proxy_idx is not None and proxy_idx >= 0:
            result["proxy_index"] = proxy_idx
            if proxy_idx < len(self.proxies):
                proxy_url = self.proxies[proxy_idx]
                if "@" in proxy_url:
                    ip_part = proxy_url.split("@")[1].split(":")[0]
                    result["proxy_ip"] = ip_part
        # update existing entry if already present
        updated = False
        for idx, existing in enumerate(self.results):
            if self._extract_key_from_entry(existing) == key:
                self.results[idx] = result
                updated = True
                break
        if not updated:
            self.results.append(result)
        self.completed_keys.add(key)
        if self._db_conn:
            try:
                if self.single_mode:
                    self._enqueue_single_job(mod1, avg_price_div, avg_price_chaos, recorded_at)
                else:
                    self._enqueue_double_job(mod1, mod2, avg_price_div, avg_price_chaos, recorded_at)
            except Exception as exc:
                self._emit_debug(f"Database persistence enqueue failed for {key}: {exc}")

    def stop(self):
        self.running = False
        self.paused = False
        if self.loop and self.queue and not self._stop_tokens_sent:
            self._stop_tokens_sent = True
            self._emit_debug("Stop requested; signaling workers to exit.")
            try:
                asyncio.run_coroutine_threadsafe(self._inject_stop_tokens(), self.loop)
            except Exception as exc:
                self._emit_debug(f"Failed to inject stop tokens: {exc}")

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def _enqueue_single_job(self, mod_id, avg_div, avg_chaos, recorded_at):
        if not self.db_queue or not self.batch_id:
            return
        job = {
            "kind": "single",
            "batch_id": self.batch_id,
            "mod_id": mod_id,
            "mod_name": MOD_NAMES.get(mod_id, str(mod_id)),
            "avg_div": float(avg_div),
            "avg_chaos": float(avg_chaos),
            "rate": float(self.divine_to_chaos_rate),
            "listings": int(self.listing_count),
            "recorded_at": recorded_at,
        }
        self.db_queue.put_nowait(job)

    def _enqueue_double_job(self, mod1, mod2, avg_div, avg_chaos, recorded_at):
        if not self.db_queue or not self.batch_id or mod2 is None:
            return
        if mod1 > mod2:
            mod1, mod2 = mod2, mod1
        job = {
            "kind": "double",
            "batch_id": self.batch_id,
            "mod1_id": mod1,
            "mod2_id": mod2,
            "mod1_name": MOD_NAMES.get(mod1, str(mod1)),
            "mod2_name": MOD_NAMES.get(mod2, str(mod2)),
            "avg_div": float(avg_div),
            "avg_chaos": float(avg_chaos),
            "rate": float(self.divine_to_chaos_rate),
            "listings": int(self.listing_count),
            "recorded_at": recorded_at,
        }
        self.db_queue.put_nowait(job)

# UI class remains similar to original; we keep it unchanged externally except filename
class PriceFetcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PoE Watcher's Eye Analyzer")
        self.resize(900, 600)

        layout = QVBoxLayout()
        self.setLayout(layout)

        button_layout = QHBoxLayout()

        self.refresh_button = QPushButton("Start Double Mod Fetch")
        self.refresh_button.clicked.connect(self.start_fetching)
        button_layout.addWidget(self.refresh_button)

        self.single_button = QPushButton("Start Single Mod Fetch")
        self.single_button.clicked.connect(self.start_single_fetching)
        button_layout.addWidget(self.single_button)

        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.pause_fetching)
        self.pause_button.setEnabled(False)
        button_layout.addWidget(self.pause_button)

        self.resume_button = QPushButton("Resume")
        self.resume_button.clicked.connect(self.resume_fetching)
        self.resume_button.setEnabled(False)
        button_layout.addWidget(self.resume_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_fetching)
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.stop_button)

        layout.addLayout(button_layout)

        self.debug_button = QPushButton("Show Debug Info")
        self.debug_button.clicked.connect(self.show_debug_info)
        layout.addWidget(self.debug_button)

        self.upload_button = QPushButton("Upload CSV to Sheets")
        self.upload_button.clicked.connect(self.manual_upload_to_sheets)
        layout.addWidget(self.upload_button)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.divine_rate = DIVINE_TO_CHAOS_RATE
        self._status_after_finish = None

        # === Configuration controls ===
        config_layout = QFormLayout()

        self.league_combo = QComboBox()
        self.league_combo.addItems(["Keepers", "Standard", "Hardcore", "SSF Standard", "SSF Hardcore"])
        self.league_combo.setCurrentText("Keepers")
        config_layout.addRow("League:", self.league_combo)

        self.listing_count_spin = QSpinBox()
        self.listing_count_spin.setMinimum(1)
        self.listing_count_spin.setMaximum(50)
        self.listing_count_spin.setValue(5)
        config_layout.addRow("Listing Count:", self.listing_count_spin)

        self.divine_rate_button = QPushButton(self._divine_rate_button_text())
        self.divine_rate_button.clicked.connect(self.configure_divine_rate)
        config_layout.addRow("Divine→Chaos:", self.divine_rate_button)

        self.proxy_checkbox = QCheckBox("Use Proxies")
        self.proxy_checkbox.setChecked(False)
        config_layout.addRow("", self.proxy_checkbox)

        self.proxy_files_button = QPushButton("Proxy Files…")
        self.proxy_files_button.clicked.connect(self.choose_proxy_files)
        config_layout.addRow("", self.proxy_files_button)

        self.auto_upload_checkbox = QCheckBox("Auto-upload to Google Sheets")
        self.auto_upload_checkbox.setChecked(False)
        config_layout.addRow("", self.auto_upload_checkbox)

        layout.addLayout(config_layout)

        # === updated table ===
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Mod 1", "Mod 2", "Avg Price (Divine)", "Avg Price (Chaos)", "Proxy"])
        layout.addWidget(self.table)

        self.debug_info = ""
        self.thread = None
        self.worker = None
        self.is_running = False
        self.proxy_states = {}

        # Load and apply persisted settings
        self._settings = load_settings()
        self._apply_settings_to_ui()

    def start_fetching(self):
        self._start_worker(single=False)

    def start_single_fetching(self):
        self._start_worker(single=True)

    def _start_worker(self, single):
        if self.is_running:
            QMessageBox.warning(self, "Already Running", "A fetch operation is already in progress. Please stop it first.")
            return

        if self.thread and self.thread.isRunning():
            self._cleanup_thread()

        self.table.setRowCount(0)
        self.debug_info = ""
        self.proxy_states = {}
        self.is_running = True
        self._status_after_finish = "Completed."

        self.refresh_button.setEnabled(False)
        self.single_button.setEnabled(False)
        self.league_combo.setEnabled(False)
        self.listing_count_spin.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        self.resume_button.setEnabled(False)

        self.thread = QThread()
        self.worker = PriceWorker()
        self.worker.single_mode = single
        self.worker.league = self.league_combo.currentText()
        self.worker.listing_count = self.listing_count_spin.value()
        self.worker.use_proxies = self.proxy_checkbox.isChecked()
        self.worker.divine_to_chaos_rate = self.divine_rate
        # pass configured proxy files into worker
        proxy_files = self._settings.get("proxy_files") or ["Webshare 10 proxies.txt", "Oxylabs proxies.txt"]
        self.worker.proxy_files = list(proxy_files)
        self.worker.auto_upload = bool(self._settings.get("auto_upload", False))
        existing_results = self.worker.prepare_for_run()
        if existing_results:
            self._populate_cached_results(existing_results)
            self.status_label.setText(f"Loaded {len(existing_results)} cached results, starting fetch...")
        else:
            self.status_label.setText("Loading...")
        self.worker.moveToThread(self.thread)

        self.worker.result_ready.connect(self.update_table)
        self.worker.status_update.connect(self.status_label.setText)
        self.worker.debug_message.connect(self.collect_debug)
        self.worker.proxy_status_update.connect(self.on_proxy_status_update)
        self.thread.finished.connect(self._on_thread_finished)

        self.thread.started.connect(self.worker.start)
        self.thread.start()

    def pause_fetching(self):
        if self.worker and self.is_running:
            self.worker.pause()
            self.status_label.setText("Paused.")
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(True)

    def resume_fetching(self):
        if self.worker and self.is_running:
            self.worker.resume()
            self.status_label.setText("Resumed.")
            self.pause_button.setEnabled(True)
            self.resume_button.setEnabled(False)

    def stop_fetching(self):
        if not self.is_running:
            return
        self._status_after_finish = "Stopped."
        if self.worker:
            self.worker.stop()
        self.status_label.setText("Stopping...")
        if self.thread and self.thread.isRunning():
            self._cleanup_thread()
        self._reset_ui_state("Stopped.")

    def _cleanup_thread(self):
        if self.thread:
            if self.worker:
                self.worker.stop()
            self.thread.quit()
            self.thread.wait(3000)
            if self.thread.isRunning():
                self.thread.terminate()
                self.thread.wait()
            self.thread = None
            self.worker = None
        self.is_running = False

    def _reset_ui_state(self, status_text=None):
        self.is_running = False
        self.refresh_button.setEnabled(True)
        self.single_button.setEnabled(True)
        self.league_combo.setEnabled(True)
        self.listing_count_spin.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)

        final_text = status_text or self._status_after_finish
        if final_text:
            self.status_label.setText(final_text)
        self._status_after_finish = None

    def _on_thread_finished(self):
        thread = self.sender()
        if isinstance(thread, QThread):
            thread.deleteLater()
        self.thread = None
        self.worker = None
        self._reset_ui_state()

    def update_table(self, price_div, price_chaos, mod1, mod2, proxy_idx):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(mod1))
        self.table.setItem(row, 1, QTableWidgetItem(mod2))
        self.table.setItem(row, 2, QTableWidgetItem(f"{price_div:.2f}" if price_div else "N/A"))
        self.table.setItem(row, 3, QTableWidgetItem(f"{price_chaos:.0f}" if price_chaos else "N/A"))
        proxy_item = QTableWidgetItem()
        proxy_item.setData(Qt.UserRole, proxy_idx)
        self.table.setItem(row, 4, proxy_item)
        self._apply_proxy_cell_style(row, proxy_idx)

    def collect_debug(self, message):
        self.debug_info += message + "\n"

    def show_debug_info(self):
        msg = QMessageBox()
        msg.setWindowTitle("Debug Info")
        msg.setIcon(QMessageBox.Information)
        text_edit = QTextEdit()
        text_edit.setPlainText(self.debug_info)
        text_edit.setReadOnly(True)
        text_edit.setMinimumSize(600, 400)
        layout = msg.layout()
        layout.addWidget(text_edit, 0, 0, 1, layout.columnCount())
        msg.exec_()

    def choose_proxy_files(self):
        start_dir = str(Path('.').resolve())
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select proxy list files",
            start_dir,
            "Text Files (*.txt);;All Files (*)",
        )
        if files:
            self._settings["proxy_files"] = list(files)
            try:
                self._save_current_settings()
            except Exception:
                pass

    def _divine_rate_button_text(self):
        return f"Current: {self.divine_rate:.2f}"

    def configure_divine_rate(self):
        value, ok = QInputDialog.getDouble(
            self,
            "Set Divine→Chaos Rate",
            "Enter new Divine→Chaos rate:",
            self.divine_rate,
            1.0,
            1000.0,
            2
        )
        if ok:
            self.divine_rate = value
            self.divine_rate_button.setText(self._divine_rate_button_text())
            if self.worker and self.is_running:
                self.status_label.setText("New rate will apply on next fetch.")
            # Save setting immediately
            try:
                self._save_current_settings()
            except Exception:
                pass

    def on_proxy_status_update(self, proxy_idx, state):
        self.proxy_states[proxy_idx] = state
        if state == "rate_limited":
            self.status_label.setText(f"Proxy {proxy_idx} hit rate limit. Waiting for retry.")
        elif state in ("error", "dead"):
            self.status_label.setText(f"Proxy {proxy_idx} error; see debug log for details.")
        self._refresh_proxy_styles()

    def _refresh_proxy_styles(self):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 4)
            if not item:
                continue
            proxy_idx = item.data(Qt.UserRole)
            self._apply_proxy_cell_style(row, proxy_idx)

    def _apply_proxy_cell_style(self, row, proxy_idx):
        proxy_item = self.table.item(row, 4)
        if proxy_item is None:
            return
        if proxy_idx is None:
            proxy_idx = -1
        state = self.proxy_states.get(proxy_idx)
        label = "Direct" if proxy_idx is None or proxy_idx < 0 else f"Proxy {proxy_idx}"
        tooltip = ""
        if state == "rate_limited":
            proxy_item.setBackground(QColor(255, 140, 140))
            tooltip = "Rate limited - retrying shortly."
            label += " (Rate limited)"
        elif state in ("error", "dead"):
            proxy_item.setBackground(QColor(255, 200, 120))
            tooltip = "Proxy error - see debug log."
            label += " (Error)"
        elif state == "cooldown":
            proxy_item.setBackground(QColor(255, 220, 140))
            tooltip = "Cooling down before retry."
            label += " (Cooldown)"
        else:
            proxy_item.setBackground(QColor(Qt.white))
        proxy_item.setForeground(QColor(Qt.black))
        proxy_item.setText(label)
        if tooltip:
            proxy_item.setToolTip(tooltip)
        else:
            proxy_item.setToolTip("")

    # === Settings helpers ===
    def _apply_settings_to_ui(self):
        # League
        league = self._settings.get("league", "Keepers")
        idx = self.league_combo.findText(league)
        if idx >= 0:
            self.league_combo.setCurrentIndex(idx)
        # Listing count
        try:
            listings = int(self._settings.get("listing_count", 5))
        except Exception:
            listings = 5
        self.listing_count_spin.setValue(max(1, min(50, listings)))
        # Divine rate
        try:
            rate = float(self._settings.get("divine_to_chaos_rate", DIVINE_TO_CHAOS_RATE))
        except Exception:
            rate = DIVINE_TO_CHAOS_RATE
        self.divine_rate = rate
        self.divine_rate_button.setText(self._divine_rate_button_text())
        # Use proxies
        use_proxies = bool(self._settings.get("use_proxies", False))
        self.proxy_checkbox.setChecked(use_proxies)
        # Auto-upload
        auto_upload = bool(self._settings.get("auto_upload", False))
        self.auto_upload_checkbox.setChecked(auto_upload)

        # Persist on simple changes
        self.league_combo.currentTextChanged.connect(lambda _=None: self._save_current_settings())
        self.listing_count_spin.valueChanged.connect(lambda _=None: self._save_current_settings())
        self.proxy_checkbox.stateChanged.connect(lambda _=None: self._save_current_settings())
        self.auto_upload_checkbox.stateChanged.connect(lambda _=None: self._save_current_settings())

    def _collect_current_settings(self):
        return {
            "league": self.league_combo.currentText(),
            "listing_count": int(self.listing_count_spin.value()),
            "divine_to_chaos_rate": float(self.divine_rate),
            "use_proxies": bool(self.proxy_checkbox.isChecked()),
            "auto_upload": bool(self.auto_upload_checkbox.isChecked()),
            "proxy_files": list(self._settings.get("proxy_files") or ["Webshare 10 proxies.txt", "Oxylabs proxies.txt"]),
        }

    def _save_current_settings(self):
        self._settings.update(self._collect_current_settings())
        save_settings(self._settings)

    def _populate_cached_results(self, results):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            mod1 = entry.get("mod1_name") or entry.get("mod1") or "-"
            mod2_value = entry.get("mod2_name") or entry.get("mod2") or "-"
            mod2 = mod2_value if mod2_value not in (None, "") else "-"
            price_div = entry.get("avg_price_divine")
            price_chaos = entry.get("avg_price_chaos")
            proxy_idx = entry.get("proxy_index")
            if isinstance(proxy_idx, str):
                try:
                    proxy_idx = int(proxy_idx)
                except ValueError:
                    proxy_idx = -1
            if proxy_idx is None:
                proxy_idx = -1
            self.update_table(price_div, price_chaos, mod1, mod2, proxy_idx)

    def closeEvent(self, event):
        # Persist settings on exit
        try:
            self._save_current_settings()
        except Exception:
            pass
        self._cleanup_thread()
        super().closeEvent(event)

    def manual_upload_to_sheets(self):
        try:
            from google_drive_sync import (
                GoogleSheetsConfigError,
                upload_csv_to_google_sheet,
            )
        except ImportError as exc:
            QMessageBox.critical(
                self,
                "Upload Error",
                f"Google client libraries missing: {exc}",
            )
            return

        start_dir = str(EXPORT_DIR.resolve())
        csv_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select CSV Export",
            start_dir,
            "CSV Files (*.csv)",
        )
        if not csv_path:
            return

        mode = self._infer_mode_from_filename(csv_path)
        if mode is None:
            mode, ok = QInputDialog.getItem(
                self,
                "Select Mode",
                "Choose which sheet to update:",
                ["single", "double"],
                0,
                False,
            )
            if not ok:
                return

        try:
            upload_csv_to_google_sheet(Path(csv_path), mode)
            QMessageBox.information(
                self,
                "Upload Complete",
                f"Uploaded {csv_path} to the {mode} sheet.",
            )
        except GoogleSheetsConfigError as exc:
            QMessageBox.critical(
                self,
                "Configuration Error",
                str(exc),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Upload Failed",
                str(exc),
            )

    def _infer_mode_from_filename(self, path):
        lower = Path(path).name.lower()
        if "single" in lower:
            return "single"
        if "double" in lower:
            return "double"
        return None

def main():
    app = QApplication(sys.argv)
    window = PriceFetcher()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
