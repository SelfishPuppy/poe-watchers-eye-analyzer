
# watchers_eye_gui_concurrent.py
# Modified PoE Watcher's Eye analyzer
# - Persistent proxy sessions
# - One async worker per proxy
# - Each proxy sends a request every 8 seconds (stable loop)
# - Uses a shared asyncio.Queue of mod combos so proxies take next available job
# - Proxies that die are retried after cooldown (60s)
# UI structure left unchanged

import sys
import os
import asyncio
import sqlite3
import aiohttp
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, QTextEdit, QHBoxLayout,
    QComboBox, QSpinBox, QFormLayout, QCheckBox, QInputDialog
)
from PyQt5.QtCore import QThread, pyqtSignal, QObject, Qt
from PyQt5.QtGui import QColor
from mod_data import MOD_COMBOS, MOD_NAMES
from watchers_eye_db import ensure_database, export_batch_to_csv

DIVINE_TO_CHAOS_RATE = 100.0
SINGLE_MODS = list(MOD_NAMES.keys())
DOUBLE_RESULTS_FILE = "watcher_prices_double.json"
SINGLE_RESULTS_FILE = "watcher_prices_single.json"
NAME_TO_MOD_ID = {name: mod_id for mod_id, name in MOD_NAMES.items()}
DATABASE_PATH = Path("data/watchers_eye.db")
EXPORT_DIR = Path("exports")

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
        self.proxy_cooldown = 60  # seconds to wait before retrying dead proxy
        self.proxy_retry_delay = 60
        self.proxy_loop_period = 10  # each proxy must perform request every 8 seconds

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
        self.export_dir = EXPORT_DIR

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
        results_file = self._current_results_file()
        loaded_results = []
        if os.path.exists(results_file):
            try:
                with open(results_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    loaded_results = data
                    for entry in data:
                        key = self._extract_key_from_entry(entry)
                        if key:
                            self.completed_keys.add(key)
                else:
                    self._emit_debug(f"Ignoring malformed results file: {results_file}")
            except Exception as exc:
                self._emit_debug(f"Failed to load cached results: {exc}")
        self.results = list(loaded_results)
        self._emit_debug(
            f"Prepared run with {len(self.completed_keys)} previously completed modifiers "
            f"(single_mode={self.single_mode}, rate={self.divine_to_chaos_rate})."
        )
        self._emit_debug(f"Starting new batch {self.batch_id} for league={self.league}.")
        return list(self.results)

    def _current_results_file(self):
        return SINGLE_RESULTS_FILE if self.single_mode else DOUBLE_RESULTS_FILE

    def _emit_debug(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        formatted = f"[{timestamp}] {message}"
        self.debug_message.emit(formatted)
        try:
            with open(self.debug_log_path, "a", encoding="utf-8") as log:
                log.write(formatted + "\n")
        except Exception:
            # avoid recursion if logging fails
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
            self._db_conn = sqlite3.connect(self.db_path)
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

    def load_proxies(self):
        try:
            with open("Webshare 10 proxies.txt", "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.proxies = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":")
                if len(parts) == 4:
                    ip, port, username, password = parts
                    proxy_url = f"http://{username}:{password}@{ip}:{port}"
                    self.proxies.append(proxy_url)
            self._emit_debug(f"Loaded {len(self.proxies)} proxies")
            return len(self.proxies) > 0
        except Exception as e:
            self._emit_debug(f"Failed to load proxies: {e}")
            return False

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
                    self.update_results_file()
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
                to_sleep = max(0, self.proxy_loop_period - elapsed)
                # if we are stopping, break early
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
                        self.update_results_file()
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
                    await asyncio.sleep(max(0.5, 1.0 - elapsed))
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
            # close any remaining proxy sessions
            for idx in list(self.proxy_sessions.keys()):
                try:
                    await self._close_proxy_session(idx)
                except Exception:
                    pass
            self._emit_debug("Worker stopped and sessions closed")
            self._finalize_batch_record()
            self._close_db_connection()
            export_path = self._export_batch_results()
            if export_path:
                self.status_update.emit(f"Exported batch data to {export_path}")

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
                    self._persist_single_price(mod1, avg_price_div, avg_price_chaos, recorded_at)
                else:
                    self._persist_double_price(mod1, mod2, avg_price_div, avg_price_chaos, recorded_at)
            except Exception as exc:
                self._emit_debug(f"Database persistence failed for {key}: {exc}")

    def update_results_file(self):
        try:
            with open(self._current_results_file(), "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            self._emit_debug(f"Saved {self._current_results_file()}")
        except Exception as e:
            self._emit_debug(f"File write error: {e}")

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

    def _persist_single_price(self, mod_id, avg_div, avg_chaos, recorded_at):
        if not self._db_conn or not self.batch_id:
            return
        try:
            self._db_conn.execute(
                """
                INSERT INTO prices_single (
                    batch_id, mod_id, mod_name, avg_divine, avg_chaos,
                    divine_to_chaos_rate, listing_count, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.batch_id,
                    mod_id,
                    MOD_NAMES.get(mod_id, str(mod_id)),
                    float(avg_div),
                    float(avg_chaos),
                    float(self.divine_to_chaos_rate),
                    int(self.listing_count),
                    recorded_at,
                ),
            )
            self._db_conn.commit()
        except Exception as exc:
            self._emit_debug(f"Failed to persist single modifier {mod_id}: {exc}")

    def _persist_double_price(self, mod1, mod2, avg_div, avg_chaos, recorded_at):
        if not self._db_conn or not self.batch_id or mod2 is None:
            return
        if mod1 > mod2:
            mod1, mod2 = mod2, mod1
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
                    self.batch_id,
                    mod1,
                    mod2,
                    MOD_NAMES.get(mod1, str(mod1)),
                    MOD_NAMES.get(mod2, str(mod2)),
                    float(avg_div),
                    float(avg_chaos),
                    float(self.divine_to_chaos_rate),
                    int(self.listing_count),
                    recorded_at,
                ),
            )
            self._db_conn.commit()
        except Exception as exc:
            self._emit_debug(f"Failed to persist double modifiers {mod1}/{mod2}: {exc}")

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
        self._cleanup_thread()
        super().closeEvent(event)

def main():
    app = QApplication(sys.argv)
    window = PriceFetcher()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
