import sys
import asyncio
import aiohttp
import json
from time import time, sleep
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel, QMessageBox, QTextEdit, QHBoxLayout
)
from PyQt5.QtCore import QThread, pyqtSignal, QObject
from mod_data import MOD_COMBOS, MOD_NAMES

SINGLE_MODS = list(MOD_NAMES.keys())

class PriceWorker(QObject):
    result_ready = pyqtSignal(float, str, str)
    status_update = pyqtSignal(str)
    debug_message = pyqtSignal(str)
    countdown_update = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.running = True
        self.paused = False
        self.single_mode = False

    def start(self):
        asyncio.run(self.sequential_fetch_loop())

    async def sequential_fetch_loop(self):
        headers = {
            "User-Agent": "poe-watchers-eye-analyzer/1.0",
            "Content-Type": "application/json"
        }
        self.debug_message.emit("=== API Debug Info ===")

        async with aiohttp.ClientSession(headers=headers) as session:
            mod_list = SINGLE_MODS if self.single_mode else MOD_COMBOS

            for mod in mod_list:
                while self.paused:
                    await asyncio.sleep(1)
                if not self.running:
                    break

                if self.single_mode:
                    mod1 = mod
                    self.status_update.emit(f"Fetching: {MOD_NAMES[mod1]}")
                    avg_price = await self.query_price_single(session, mod1)
                    self.result_ready.emit(avg_price if avg_price else 0.0, MOD_NAMES[mod1], "-")
                    self.save_to_file(mod1, None, avg_price if avg_price else 0.0)
                else:
                    mod1, mod2 = mod
                    self.status_update.emit(f"Fetching: {MOD_NAMES[mod1]} + {MOD_NAMES[mod2]}")
                    avg_price = await self.query_price(session, mod1, mod2)
                    self.result_ready.emit(avg_price if avg_price else 0.0, MOD_NAMES[mod1], MOD_NAMES[mod2])
                    self.save_to_file(mod1, mod2, avg_price if avg_price else 0.0)

                for remaining in range(10, 0, -1):
                    self.countdown_update.emit(remaining)
                    await asyncio.sleep(1)
                self.countdown_update.emit(0)

    async def query_price(self, session, mod1, mod2):
        try:
            payload = {
                "query": {
                    "status": {"option": "online"},
                    "stats": [{
                        "type": "and",
                        "filters": [
                            {"id": mod1, "disabled": False},
                            {"id": mod2, "disabled": False}
                        ]
                    }],
                    "filters": {        
                        "trade_filters": {
                            "disabled": False,
                            "filters": {
                                "price": {
                                    "option": "divine"
                                }
                            }
                        }

                    }
                },
                "sort": {"price": "asc"}
            }
            url = "https://www.pathofexile.com/api/trade/search/Mercenaries"
            self.debug_message.emit(f"\n[SEARCH] {MOD_NAMES[mod1]} + {MOD_NAMES[mod2]}\n{json.dumps(payload)}")

            async with session.post(url, json=payload) as r:
                if r.status != 200:
                    self.debug_message.emit(f"Search Error: {r.status}")
                    return None
                search_data = await r.json()

            if not search_data.get("result"):
                return None

            ids = search_data["result"][:5]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(ids)}?query={search_data['id']}"
            self.debug_message.emit(f"[FETCH] {fetch_url}")

            async with session.get(fetch_url) as r:
                if r.status != 200:
                    self.debug_message.emit(f"Fetch Error: {r.status}")
                    return None
                data = await r.json()

            prices = []
            for item in data.get("result", []):
                try:
                    price = item["listing"]["price"]
                    if price["currency"] == "divine":
                        prices.append(price["amount"])
                except KeyError:
                    continue

            return sum(prices) / len(prices) if prices else None

        except Exception as e:
            self.debug_message.emit(f"Exception: {str(e)}")
            return None

    async def query_price_single(self, session, mod1):
        try:
            payload = {
                "query": {
                    "status": {"option": "online"},
                    "stats": [{
                        "type": "and",
                        "filters": [
                            {"id": mod1, "disabled": False}
                        ]
                    }],
                    "filters": {
                        "misc_filters": {
                            "disabled": False,
                            "filters": {
                                "corrupted": {"option": "false"},
                                "ilvl": {"min": 86}
                            }
                        },
                        
                        "trade_filters": {
                            "disabled": False,
                            "filters": {
                                "price": {
                                    "option": "divine"
                                }
                            }
                        }

                    }
                },
                "sort": {"price": "asc"}
            }
            url = "https://www.pathofexile.com/api/trade/search/Mercenaries"
            self.debug_message.emit(f"\n[SEARCH SINGLE] {MOD_NAMES[mod1]}\n{json.dumps(payload)}")

            async with session.post(url, json=payload) as r:
                if r.status != 200:
                    self.debug_message.emit(f"Search Error: {r.status}")
                    return None
                search_data = await r.json()

            if not search_data.get("result"):
                return None

            ids = search_data["result"][:5]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(ids)}?query={search_data['id']}"
            self.debug_message.emit(f"[FETCH SINGLE] {fetch_url}")

            async with session.get(fetch_url) as r:
                if r.status != 200:
                    self.debug_message.emit(f"Fetch Error: {r.status}")
                    return None
                data = await r.json()

            prices = []
            for item in data.get("result", []):
                try:
                    price = item["listing"]["price"]
                    if price["currency"] == "divine":
                        prices.append(price["amount"])
                except KeyError:
                    continue

            return sum(prices) / len(prices) if prices else None

        except Exception as e:
            self.debug_message.emit(f"Exception: {str(e)}")
            return None

    def save_to_file(self, mod1, mod2, avg_price):
        entry = {
            "mod1": MOD_NAMES[mod1],
            "mod2": MOD_NAMES[mod2] if mod2 else None,
            "avg_price": round(avg_price, 2)
        }
        try:
            with open("watcher_prices.json", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.debug_message.emit(f"File write error: {str(e)}")

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class PriceFetcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PoE Watcher's Eye Analyzer")
        self.resize(800, 600)

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
        button_layout.addWidget(self.pause_button)

        self.resume_button = QPushButton("Resume")
        self.resume_button.clicked.connect(self.resume_fetching)
        button_layout.addWidget(self.resume_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_fetching)
        button_layout.addWidget(self.stop_button)

        layout.addLayout(button_layout)

        self.debug_button = QPushButton("Show Debug Info")
        self.debug_button.clicked.connect(self.show_debug_info)
        layout.addWidget(self.debug_button)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.countdown_label = QLabel("Waiting: 0s")
        layout.addWidget(self.countdown_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Mod 1", "Mod 2", "Avg Price (Divine)"])
        layout.addWidget(self.table)

        self.debug_info = ""
        self.thread = None
        self.worker = None

    def start_fetching(self):
        self._start_worker(single=False)

    def start_single_fetching(self):
        self._start_worker(single=True)

    def _start_worker(self, single):
        self.table.setRowCount(0)
        self.status_label.setText("Loading...")

        self.thread = QThread()
        self.worker = PriceWorker()
        self.worker.single_mode = single
        self.worker.moveToThread(self.thread)

        self.worker.result_ready.connect(self.update_table)
        self.worker.status_update.connect(self.status_label.setText)
        self.worker.debug_message.connect(self.collect_debug)
        self.worker.countdown_update.connect(self.update_countdown)

        self.thread.started.connect(self.worker.start)
        self.thread.start()

    def pause_fetching(self):
        if self.worker:
            self.worker.pause()
            self.status_label.setText("Paused.")

    def resume_fetching(self):
        if self.worker:
            self.worker.resume()
            self.status_label.setText("Resumed.")

    def stop_fetching(self):
        if self.worker:
            self.worker.stop()
            self.status_label.setText("Stopping...")

    def update_table(self, price, mod1, mod2):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(mod1))
        self.table.setItem(row, 1, QTableWidgetItem(mod2))
        self.table.setItem(row, 2, QTableWidgetItem(f"{price:.2f}" if price else "N/A"))

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
