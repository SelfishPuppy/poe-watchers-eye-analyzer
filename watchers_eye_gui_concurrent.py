
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
import aiohttp
import json
import time
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, QTextEdit, QHBoxLayout,
    QComboBox, QSpinBox, QFormLayout, QCheckBox
)
from PyQt5.QtCore import QThread, pyqtSignal, QObject
from mod_data import MOD_COMBOS, MOD_NAMES

DIVINE_TO_CHAOS_RATE = 100.0
SINGLE_MODS = list(MOD_NAMES.keys())

class PriceWorker(QObject):
    result_ready = pyqtSignal(float, float, str, str, int)
    status_update = pyqtSignal(str)
    debug_message = pyqtSignal(str)
    countdown_update = pyqtSignal(int)

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
        self._reaper_task = None
        self._proxy_tasks = []
        self._main_task = None
        self.loop = None

        # concurrency controls
        self.max_direct_workers = 4

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
            self.debug_message.emit(f"Loaded {len(self.proxies)} proxies")
            return len(self.proxies) > 0
        except Exception as e:
            self.debug_message.emit(f"Failed to load proxies: {e}")
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
        self.debug_message.emit(f"Created persistent session for proxy {idx}")
        return session

    async def _close_proxy_session(self, idx):
        sess = self.proxy_sessions.get(idx)
        if sess:
            try:
                await sess.close()
            except Exception as e:
                self.debug_message.emit(f"Error closing session {idx}: {e}")
        self.proxy_sessions.pop(idx, None)
        self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}

    async def _fill_queue(self):
        """Populate the queue with mod combos. We'll cycle them by re-adding after processing."""
        await asyncio.sleep(0)  # allow event loop scheduling
        mod_list = SINGLE_MODS if self.single_mode else MOD_COMBOS
        # put all items initially
        for m in mod_list:
            await self.queue.put(m)
        self.debug_message.emit(f"Queue populated with {len(mod_list)} items")

    async def _proxy_worker(self, idx):
        """Worker that runs perpetually for a single proxy.
        Takes next mod combo from queue, performs request, then sleeps so the interval between requests is >= period."""
        proxy_url = self.proxies[idx] if idx < len(self.proxies) else None
        backoff = 1
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
                self.debug_message.emit(f"Proxy {idx} session create failed: {e}")
                self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
                await asyncio.sleep(self.proxy_retry_delay)
                continue

            # skip if marked dead and not cooled down
            status = self.proxy_status.get(idx, {"status":"ok","dead_since":None})
            if status["status"] == "dead":
                dead_since = status.get("dead_since") or 0
                if time.time() - dead_since < self.proxy_cooldown:
                    await asyncio.sleep(1)
                    continue
                else:
                    # retry
                    self.debug_message.emit(f"Retrying proxy {idx} after cooldown")
                    try:
                        await self._close_proxy_session(idx)
                    except Exception:
                        pass
                    try:
                        await self._ensure_proxy_session(idx)
                        status = {"status":"ok","dead_since":None}
                        self.proxy_status[idx] = status
                    except Exception as e:
                        self.debug_message.emit(f"Proxy {idx} still failing on recreate: {e}")
                        self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
                        await asyncio.sleep(self.proxy_retry_delay)
                        continue

            # take a job
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                # nothing to do, loop sleep
                await asyncio.sleep(1)
                continue

            start_ts = time.time()
            try:
                if self.single_mode:
                    mod1 = item
                    avg_div, avg_chaos = await self._query_price_single_impl(session, mod1, proxy_url, proxy_idx=idx)
                    m1_name = MOD_NAMES.get(mod1, str(mod1))
                    m2_name = "-"
                else:
                    mod1, mod2 = item
                    avg_div, avg_chaos = await self._query_price_impl(session, mod1, mod2, proxy_url, proxy_idx=idx)
                    m1_name = MOD_NAMES.get(mod1, str(mod1))
                    m2_name = MOD_NAMES.get(mod2, str(mod2))
                if avg_div is not None:
                    self.result_ready.emit(avg_div, avg_chaos, m1_name, m2_name, idx)
                    self.add_result(mod1, mod2 if not self.single_mode else None, avg_div, avg_chaos, idx)
                    self.update_results_file()
                else:
                    self.debug_message.emit(f"Proxy {idx} returned no price for {m1_name} {m2_name}")
                # re-add job to queue end so combos are cycled
                
                backoff = 1
            except Exception as e:
                self.debug_message.emit(f"Proxy {idx} task error: {e}")
                # mark dead and close session
                self.proxy_status[idx] = {"status": "dead", "dead_since": time.time()}
                try:
                    await self._close_proxy_session(idx)
                except Exception:
                    pass
            finally:
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
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.5)
                    continue
                start_ts = time.time()
                try:
                    if self.single_mode:
                        mod1 = item
                        avg_div, avg_chaos = await self._query_price_single_impl(session, mod1, proxy_url=None, proxy_idx=None)
                        m1_name = MOD_NAMES.get(mod1, str(mod1))
                        m2_name = "-"
                    else:
                        mod1, mod2 = item
                        avg_div, avg_chaos = await self._query_price_impl(session, mod1, mod2, proxy_url=None, proxy_idx=None)
                        m1_name = MOD_NAMES.get(mod1, str(mod1))
                        m2_name = MOD_NAMES.get(mod2, str(mod2))
                    if avg_div is not None:
                        self.result_ready.emit(avg_div, avg_chaos, m1_name, m2_name, -1)
                        self.add_result(mod1, mod2 if not self.single_mode else None, avg_div, avg_chaos, None)
                        self.update_results_file()
                    else:
                        self.debug_message.emit(f"Direct worker returned no price for {m1_name} {m2_name}")
                    # requeue
                    
                except Exception as e:
                    self.debug_message.emit(f"Direct worker error: {e}")
                finally:
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
        await self._fill_queue()

        # if proxies requested, ensure they are loaded and create workers per proxy
        if self.use_proxies:
            if not self.load_proxies():
                self.debug_message.emit("No proxies loaded, falling back to direct mode.")
                self.use_proxies = False

        tasks = []
        if self.use_proxies:
            # create proxy tasks
            for idx in range(len(self.proxies)):
                # initialize status as ok
                self.proxy_status[idx] = {"status":"ok","dead_since":None}
                # do not pre-create session yet; lazy create in worker
                t = asyncio.create_task(self._proxy_worker(idx))
                tasks.append(t)
                self._proxy_tasks.append(t)
            self.debug_message.emit(f"Started {len(tasks)} proxy workers")
        else:
            # create a pool of direct workers
            for _ in range(self.max_direct_workers):
                t = asyncio.create_task(self._direct_worker())
                tasks.append(t)
            self.debug_message.emit(f"Started {len(tasks)} direct workers")

        # keep main alive until stopped
        try:
            while self.running:
                await asyncio.sleep(0.5)
        finally:
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
            self.debug_message.emit("Worker stopped and sessions closed")

    def start(self):
        # create and set event loop for thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.start_async())
        except Exception as e:
            self.debug_message.emit(f"Event loop error: {e}")
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
            self.debug_message.emit(f"[SEARCH] proxy={proxy_idx} {MOD_NAMES.get(mod1)} + {MOD_NAMES.get(mod2)}")

            try:
                async with session.post(url, json=payload, proxy=proxy_url) as r:
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        self.debug_message.emit(f"Rate limit hit. Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        return await self._query_price_impl(session, mod1, mod2, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self.debug_message.emit(f"Search Error: {r.status}")
                        return None, None
                    search_data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.debug_message.emit(f"Network error (search): {e}")
                raise

            if not search_data.get("result"):
                return None, None

            ids = search_data["result"][:self.listing_count]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(ids)}?query={search_data['id']}"
            self.debug_message.emit(f"[FETCH] {fetch_url}")

            try:
                async with session.get(fetch_url, proxy=proxy_url) as r:
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        self.debug_message.emit(f"Rate limit hit (fetch). Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        return await self._query_price_impl(session, mod1, mod2, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self.debug_message.emit(f"Fetch Error: {r.status}")
                        return None, None
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.debug_message.emit(f"Network error (fetch): {e}")
                raise

            prices_divine = []
            prices_chaos = []
            for item in data.get("result", []):
                listing = item["listing"]["price"]
                amount = listing["amount"]
                currency = listing["currency"]
                if currency == "divine":
                    prices_divine.append(amount)
                    prices_chaos.append(amount * DIVINE_TO_CHAOS_RATE)
                elif currency == "chaos":
                    prices_chaos.append(amount)
                    prices_divine.append(amount / DIVINE_TO_CHAOS_RATE)

            if not prices_divine:
                return None, None

            avg_divine = sum(prices_divine) / len(prices_divine)
            avg_chaos = sum(prices_chaos) / len(prices_chaos)
            self.debug_message.emit(f"[AVG] {avg_divine:.2f} div")
            return avg_divine, avg_chaos
        except Exception as e:
            self.debug_message.emit(f"Query exception: {e}")
            raise

    async def _query_price_single_impl(self, session, mod1, proxy_url=None, proxy_idx=None, retry=True):
        if not self.running:
            return None, None
        try:
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
            self.debug_message.emit(f"[SEARCH SINGLE] proxy={proxy_idx} {MOD_NAMES.get(mod1)}")

            try:
                async with session.post(url, json=payload, proxy=proxy_url) as r:
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        await asyncio.sleep(wait_time)
                        return await self._query_price_single_impl(session, mod1, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self.debug_message.emit(f"Search Error: {r.status}")
                        return None, None
                    search_data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.debug_message.emit(f"Network error (search single): {e}")
                raise

            if not search_data.get("result"):
                return None, None

            ids = search_data["result"][:self.listing_count]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(ids)}?query={search_data['id']}"

            try:
                async with session.get(fetch_url, proxy=proxy_url) as r:
                    if r.status == 429:
                        wait_time = int(r.headers.get("Retry-After", 10))
                        await asyncio.sleep(wait_time)
                        return await self._query_price_single_impl(session, mod1, proxy_url, proxy_idx, retry)
                    if r.status != 200:
                        self.debug_message.emit(f"Fetch Error: {r.status}")
                        return None, None
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.debug_message.emit(f"Network error (fetch single): {e}")
                raise

            prices_divine = []
            prices_chaos = []
            for item in data.get("result", []):
                listing = item["listing"]["price"]
                amount = listing["amount"]
                currency = listing["currency"]
                if currency == "divine":
                    prices_divine.append(amount)
                    prices_chaos.append(amount * DIVINE_TO_CHAOS_RATE)
                elif currency == "chaos":
                    prices_chaos.append(amount)
                    prices_divine.append(amount / DIVINE_TO_CHAOS_RATE)

            if not prices_divine:
                return None, None

            avg_divine = sum(prices_divine) / len(prices_divine)
            avg_chaos = sum(prices_chaos) / len(prices_chaos)
            self.debug_message.emit(f"[AVG SINGLE] {avg_divine:.2f} div")
            return avg_divine, avg_chaos
        except Exception as e:
            self.debug_message.emit(f"Query single exception: {e}")
            raise

    # ---- Result bookkeeping ----
    def add_result(self, mod1, mod2, avg_price_div, avg_price_chaos, proxy_idx=None):
        result = {
            "mod1": MOD_NAMES.get(mod1, str(mod1)),
            "mod2": MOD_NAMES.get(mod2, str(mod2)) if mod2 else None,
            "avg_price_divine": round(avg_price_div, 2),
            "avg_price_chaos": round(avg_price_chaos, 1)
        }
        if proxy_idx is not None and proxy_idx >= 0:
            result["proxy_index"] = proxy_idx
            if proxy_idx < len(self.proxies):
                proxy_url = self.proxies[proxy_idx]
                if "@" in proxy_url:
                    ip_part = proxy_url.split("@")[1].split(":")[0]
                    result["proxy_ip"] = ip_part
        self.results.append(result)

    def update_results_file(self):
        try:
            with open("watcher_prices.json", "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
            self.debug_message.emit("Saved watcher_prices.json")
        except Exception as e:
            self.debug_message.emit(f"File write error: {e}")

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

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

        self.countdown_label = QLabel("Waiting: 0s")
        layout.addWidget(self.countdown_label)

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
        self.status_label.setText("Loading...")
        self.is_running = True

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
        self.worker.moveToThread(self.thread)

        self.worker.result_ready.connect(self.update_table)
        self.worker.status_update.connect(self.status_label.setText)
        self.worker.debug_message.connect(self.collect_debug)
        self.worker.countdown_update.connect(self.update_countdown)
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
        if self.worker and self.is_running:
            self.worker.stop()
            self.status_label.setText("Stopping... (will exit after current request completes)")

        if self.thread is None or not self.thread.isRunning():
            self._reset_ui_state()

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

    def _reset_ui_state(self):
        self.is_running = False
        self.refresh_button.setEnabled(True)
        self.single_button.setEnabled(True)
        self.league_combo.setEnabled(True)
        self.listing_count_spin.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)

        if self.worker and not self.worker.running:
            self.status_label.setText("Stopped.")
        else:
            self.status_label.setText("Completed.")

    def _on_thread_finished(self):
        self._reset_ui_state()

    def update_table(self, price_div, price_chaos, mod1, mod2, proxy_idx):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(mod1))
        self.table.setItem(row, 1, QTableWidgetItem(mod2))
        self.table.setItem(row, 2, QTableWidgetItem(f"{price_div:.2f}" if price_div else "N/A"))
        self.table.setItem(row, 3, QTableWidgetItem(f"{price_chaos:.0f}" if price_chaos else "N/A"))
        proxy_display = f"Proxy {proxy_idx}" if proxy_idx >= 0 else "Direct"
        self.table.setItem(row, 4, QTableWidgetItem(proxy_display))

    def update_countdown(self, seconds):
        self.countdown_label.setText(f"Waiting: {seconds}s")

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

def main():
    app = QApplication(sys.argv)
    window = PriceFetcher()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
