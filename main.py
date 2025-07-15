import sys
import asyncio
import aiohttp
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel
)
from PyQt5.QtCore import QTimer, pyqtSlot
from mod_data import MOD_COMBOS, MOD_NAMES
import json

class PriceFetcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PoE Watcher's Eye Analyzer")
        self.resize(800, 600)
        layout = QVBoxLayout()

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_data)
        layout.addWidget(self.refresh_button)

        self.debug_button = QPushButton("Show Debug Info")
        self.debug_button.clicked.connect(self.show_debug_info)
        layout.addWidget(self.debug_button)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Mod 1", "Mod 2", "Avg Price (Divine)"])
        layout.addWidget(self.table)

        self.setLayout(layout)
        self.loop = asyncio.new_event_loop()
        self.debug_info = ""
        self.task = None
        self.request_count = 0
        self.minute_start = None

    def refresh_data(self):
        self.status_label.setText("Querying trade site...")
        self.table.setRowCount(0)
        self.debug_info = "=== API Debug Information ===\n"
        QTimer.singleShot(0, self.run_async_task)

    def show_debug_info(self):
        from PyQt5.QtWidgets import QMessageBox, QTextEdit
        msg = QMessageBox()
        msg.setWindowTitle("API Debug Info")
        msg.setIcon(QMessageBox.Information)
        
        text_edit = QTextEdit()
        text_edit.setPlainText(self.debug_info)
        text_edit.setReadOnly(True)
        text_edit.setMinimumSize(600, 400)
        
        layout = msg.layout()
        layout.addWidget(text_edit, 0, 0, 1, layout.columnCount())
        msg.exec_()

    def run_async_task(self):
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self.run_price_checks())
        self.loop.run_until_complete(self.task)

    @pyqtSlot(float, str, str)
    def update_table_slot(self, avg_price, mod1_name, mod2_name):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(mod1_name))
        self.table.setItem(row, 1, QTableWidgetItem(mod2_name))
        self.table.setItem(row, 2, QTableWidgetItem(f"{avg_price:.2f}" if avg_price is not None else "N/A"))
        self.table.viewport().update()

    async def run_price_checks(self):
        from time import time
        headers = {
            "User-Agent": "poe-watchers-eye-analyzer/1.0 (contact: weakness.of.power@gmail.com)",
            "Content-Type": "application/json"
        }
        
        self.request_count = 0
        self.minute_start = time()
        
        async with aiohttp.ClientSession(headers=headers) as session:
            for i, (mod1, mod2) in enumerate(MOD_COMBOS):
                self.debug_info += f"\n\nProcessing mods: {MOD_NAMES[mod1]} + {MOD_NAMES[mod2]}\n"
                self.status_label.setText(f"Processing ({i+1}/{len(MOD_COMBOS)}): {MOD_NAMES[mod1]}")
                
                avg_price = await self.query_price(session, mod1, mod2)
                # Use QTimer to update UI from async context
                QTimer.singleShot(0, lambda p=avg_price, m1=MOD_NAMES[mod1], m2=MOD_NAMES[mod2]: 
                                self.update_table_slot(p, m1, m2))
                
                # Rate limiting - ensure 15 requests per minute
                await self.rate_limit()
                
                if i < len(MOD_COMBOS) - 1:
                    self.status_label.setText(f"Waiting for rate limit... ({i+1}/{len(MOD_COMBOS)})")
        
        self.status_label.setText("Done.")

    async def rate_limit(self):
        from time import time
        self.request_count += 2  # Each mod pair = 2 requests (search + fetch)
        
        if self.request_count >= 14:  # Slightly less than 15 to be safe
            elapsed = time() - self.minute_start
            if elapsed < 60:
                await asyncio.sleep(60 - elapsed)
            self.request_count = 0
            self.minute_start = time()
        else:
            # Minimal delay to space out requests, but still respect overall rate
            await asyncio.sleep(60 / 7.5 - (time() - self.minute_start) % (60 / 7.5) if self.request_count < 14 else 0)

    async def query_price(self, session, mod1, mod2):
        try:
            # Step 1: Search request
            search_payload = {
                "query": {
                    "status": {"option": "online"},
                    "stats": [{
                        "type": "and",
                        "filters": [{"id": mod1, "disabled": False}, {"id": mod2, "disabled": False}]
                    }]
                },
                "sort": {"price": "asc"}
            }
            
            search_url = "https://www.pathofexile.com/api/trade/search/Mercenaries"
            self.debug_info += f"\n[SEARCH REQUEST] URL: {search_url}\nPayload: {json.dumps(search_payload, indent=2)}"
            
            async with session.post(search_url, json=search_payload) as r:
                self.debug_info += f"\n[SEARCH RESPONSE] Status: {r.status}"
                if r.status != 200:
                    error_text = await r.text()
                    self.debug_info += f"\nError: {error_text}"
                    return None
                    
                if r.content_type != 'application/json':
                    self.debug_info += f"\nUnexpected content type: {r.content_type}"
                    return None
                    
                search_data = await r.json()
                self.debug_info += f"\nSearch Results: {len(search_data.get('result', []))} items found"
                
            if not search_data.get("result"):
                return None
                
            # Step 2: Fetch listings
            result_ids = search_data["result"][:5]
            fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(result_ids)}?query={search_data['id']}"
            self.debug_info += f"\n[FETCH REQUEST] URL: {fetch_url}"
            
            async with session.get(fetch_url) as r:
                self.debug_info += f"\n[FETCH RESPONSE] Status: {r.status}"
                if r.status != 200:
                    error_text = await r.text()
                    self.debug_info += f"\nError: {error_text}"
                    return None
                    
                if r.content_type != 'application/json':
                    self.debug_info += f"\nUnexpected content type: {r.content_type}"
                    return None
                    
                listings = await r.json()
                self.debug_info += f"\nListings Received: {len(listings.get('result', []))}"
                
            # Process prices
            prices = []
            for item in listings.get("result", []):
                try:
                    price = item["listing"]["price"]
                    if price["currency"] == "divine":
                        prices.append(price["amount"])
                        self.debug_info += f"\n- Found price: {price['amount']} divine"
                except KeyError:
                    continue
                    
            if prices:
                avg = sum(prices) / len(prices)
                self.debug_info += f"\nAverage price: {avg:.2f} divine"
                return avg
            return None
            
        except Exception as e:
            self.debug_info += f"\nException in query_price: {str(e)}"
            return None

def main():
    app = QApplication(sys.argv)
    fetcher = PriceFetcher()
    fetcher.show()
    app.exec_()

if __name__ == "__main__":
    main()