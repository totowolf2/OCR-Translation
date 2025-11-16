import json
import logging
import threading
import tkinter as tk
from pathlib import Path
from time import monotonic
from tkinter import messagebox, scrolledtext

from PIL import ImageGrab
import pytesseract
from deep_translator import GoogleTranslator
import keyboard
from pytesseract import TesseractNotFoundError

# ------------------ Logging Setup ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------ Tesseract Path ------------------
# ถ้า tesseract ไม่อยู่ใน PATH ให้เปิดบรรทัดด้านล่างแล้วใส่ path ของนายเอง
# ตัวอย่างบน Windows:
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

APP_DIR = Path(__file__).resolve().parent
POSITIONS_PATH = APP_DIR / "watch_positions.json"


class OcrTranslatorApp:
    """
    OCR English → Translate to Thai.

    Features:
    - Global hotkeys:
        Ctrl+Shift+Q : เลือกพื้นที่ครั้งเดียว → OCR → Translate
        Ctrl+Shift+W : เลือกพื้นที่เพื่อเฝ้าดู (watch mode)
        Ctrl+Shift+E : หยุด watch mode
    - GUI แบ่งบน/ล่างเท่า ๆ กัน:
        บน : OCR English
        ล่าง: แปลไทย
      ถ้าข้อความยาว → เลื่อนเอา (scrollbar) ไม่ขยายกล่อง
    - Auto adjust font size ตามจำนวนตัวอักษร (มี min/max กำหนด)
    """

    def _ensure_tesseract(self):
      try:
          pytesseract.get_tesseract_version()
      except TesseractNotFoundError:
          messagebox.showerror(
              "Tesseract missing",
              "Install Tesseract OCR and set pytesseract.pytesseract.tesseract_cmd.\n"
              "See README for instructions."
          )
          raise

    def __init__(self):
        # ---- Main window ----
        self._ensure_tesseract()
        self.root = tk.Tk()
        self.root.title("OCR EN → TH")
        self.root.geometry("800x500")
        self.root.configure(bg="#f5f5f5")

        # ปิดหน้าต่างแล้วหยุด thread watch ด้วย
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- Selection overlay state ----
        self.sel_start_x = None
        self.sel_start_y = None
        self.sel_rect = None
        self.selection_window = None
        self.selection_canvas = None
        self.after_selection_action = None  # callback after region selected

        # ---- Watch mode state ----
        self.watch_bbox = None
        self.watch_thread = None
        self.watch_stop_event = threading.Event()
        self.last_watch_text = ""

        # ---- Overlay state ----
        self.overlay_bbox = None
        self.overlay_window = None
        self.overlay_label = None

        # ---- Layout helpers ----
        self.center_paned = None

        # ---- Mode flags ----
        self.watch_mode_active = False
        self.history_reset_interval = 60.0  # seconds of inactivity before clearing history
        self.last_history_timestamp = None

        # ---- Saved position state ----
        self.saved_watch_bbox = None
        self.saved_overlay_bbox = None
        self.saved_status_label = None
        self.positions_path = POSITIONS_PATH

        # ---- Build UI & hotkeys ----
        self._build_ui()
        self._load_saved_positions()
        self._register_hotkey()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        """Create UI: left (OCR) and right (translation) text boxes with adjustable splitter."""
        hotkey_label = tk.Label(
            self.root,
            text="Hotkeys: Ctrl+Shift+Q = แปลครั้งเดียว | Ctrl+Shift+W = เฝ้าพื้นที่ | Ctrl+Shift+E = หยุดเฝ้า",
            bg="#f5f5f5",
            fg="#333333",
        )
        hotkey_label.pack(pady=5)

        saved_frame = tk.Frame(self.root, bg="#f5f5f5")
        saved_frame.pack(fill=tk.X, padx=10, pady=(0, 5))

        tk.Button(
            saved_frame,
            text="ใช้ตำแหน่งที่บันทึก",
            command=self._start_watch_from_saved,
        ).pack(side=tk.LEFT, padx=(0, 5))

        tk.Button(
            saved_frame,
            text="แก้ไข/เลือกใหม่",
            command=lambda: self.root.after(0, self._start_watch_workflow),
        ).pack(side=tk.LEFT, padx=(0, 5))

        self.saved_status_label = tk.Label(
            saved_frame,
            text="ยังไม่มีตำแหน่งที่บันทึก",
            bg="#f5f5f5",
            fg="#555555",
        )
        self.saved_status_label.pack(side=tk.LEFT)

        # Paned window for adjustable split (left/right)
        self.center_paned = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            sashwidth=6,
            sashrelief=tk.RAISED,
            bg="#f5f5f5",
            bd=0,
        )
        self.center_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # -------- Left (OCR) --------
        left_frame = tk.Frame(self.center_paned, bg="#f5f5f5")
        top_label = tk.Label(left_frame, text="Original (OCR EN)", bg="#f5f5f5")
        top_label.pack(anchor="w", padx=5, pady=(5, 0))

        self.text_original = scrolledtext.ScrolledText(
            left_frame,
            wrap=tk.WORD,   # auto wrap lines
        )
        self.text_original.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        # -------- Right (Translation) --------
        right_frame = tk.Frame(self.center_paned, bg="#f5f5f5")
        bottom_label = tk.Label(right_frame, text="Translation (TH)", bg="#f5f5f5")
        bottom_label.pack(anchor="w", padx=5, pady=(5, 0))

        self.text_translation = scrolledtext.ScrolledText(
            right_frame,
            wrap=tk.WORD,
        )
        self.text_translation.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        self.center_paned.add(left_frame, minsize=200)
        self.center_paned.add(right_frame, minsize=200)
        self.root.after(150, self._set_initial_split)

    def _set_initial_split(self, ratio: float = 0.5):
        """Place the paned-window sash so both panes start 50:50."""
        if self.center_paned is None:
            return

        total_width = self.center_paned.winfo_width()
        if total_width <= 1:
            # window not rendered yet, retry shortly
            self.root.after(150, lambda: self._set_initial_split(ratio))
            return

        sash_x = int(total_width * ratio)
        self.center_paned.sash_place(0, sash_x, 0)

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------
    def _register_hotkey(self):
        """Register global hotkeys for capture and watch."""
        keyboard.add_hotkey("ctrl+shift+q", self._on_hotkey_single)
        keyboard.add_hotkey("ctrl+shift+w", self._on_hotkey_watch)
        keyboard.add_hotkey("ctrl+shift+e", self._on_hotkey_stop_watch)
        logger.info("Hotkeys registered: Q(single), W(watch), E(stop watch)")

    def _load_saved_positions(self):
        """Load saved watch/overlay positions from disk."""
        try:
            if self.positions_path.exists():
                data = json.loads(self.positions_path.read_text(encoding="utf-8"))
                watch = data.get("watch_bbox")
                overlay = data.get("overlay_bbox")
                if watch:
                    self.saved_watch_bbox = tuple(watch)
                if overlay:
                    self.saved_overlay_bbox = tuple(overlay)
                logger.info("Loaded saved positions from %s", self.positions_path)
        except Exception as exc:
            logger.exception("Failed to load saved positions: %s", exc)
        finally:
            self._update_saved_status_label()

    def _save_watch_positions(self):
        """Persist current watch/overlay regions."""
        if self.watch_bbox is None or self.overlay_bbox is None:
            return

        self.saved_watch_bbox = tuple(self.watch_bbox)
        self.saved_overlay_bbox = tuple(self.overlay_bbox)

        data = {
            "watch_bbox": list(self.saved_watch_bbox),
            "overlay_bbox": list(self.saved_overlay_bbox),
        }
        try:
            self.positions_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Saved watch positions to %s", self.positions_path)
        except Exception as exc:
            logger.exception("Unable to save positions: %s", exc)
            messagebox.showwarning(
                "บันทึกตำแหน่งไม่สำเร็จ",
                f"ไม่สามารถบันทึกไฟล์ {self.positions_path} ได้:\n{exc}",
            )
        finally:
            self._update_saved_status_label()

    def _update_saved_status_label(self):
        """Refresh label that shows saved-position info."""
        if self.saved_status_label is None:
            return

        if self.saved_watch_bbox and self.saved_overlay_bbox:
            self.saved_status_label.config(
                text=(
                    f"watch: {self.saved_watch_bbox} | overlay: {self.saved_overlay_bbox}"
                )
            )
        else:
            self.saved_status_label.config(text="ยังไม่มีตำแหน่งที่บันทึก")

    def _start_watch_from_saved(self):
        """Start watch mode directly from saved regions."""
        if not (self.saved_watch_bbox and self.saved_overlay_bbox):
            messagebox.showinfo(
                "ยังไม่มีตำแหน่ง",
                "ยังไม่ได้บันทึกตำแหน่ง watch / overlay กรุณาเลือกก่อน",
            )
            return

        self._stop_watch()
        self.watch_bbox = tuple(self.saved_watch_bbox)
        self.overlay_bbox = tuple(self.saved_overlay_bbox)
        self._create_overlay_window()
        self._update_overlay_text("รอข้อความจาก watch ...")
        self._reset_translation_history()
        self._start_watch_after_selection(self.watch_bbox)

    def _on_hotkey_single(self):
        """Single capture mode."""
        logger.info("Hotkey single capture pressed")
        self.after_selection_action = self._process_single_capture
        self.root.after(0, self._start_region_selection)

    def _on_hotkey_watch(self):
        """Start region selection for watch mode."""
        logger.info("Hotkey watch pressed")
        self.root.after(0, self._start_watch_workflow)

    def _start_watch_workflow(self):
        """Two-step selection: watch area then overlay display area."""
        self._stop_watch()
        self.watch_bbox = None
        self.overlay_bbox = None
        self._reset_translation_history()
        self.after_selection_action = self._on_watch_area_selected
        self._update_translation_text("ลากเพื่อเลือกพื้นที่ที่ต้องการให้ OCR เฝ้าดู")
        self._start_region_selection()

    def _on_watch_area_selected(self, bbox):
        """Handle the first selection (screen region to read from)."""
        logger.info("Watch capture area selected: %s", bbox)
        self.watch_bbox = bbox
        self.after_selection_action = self._on_overlay_area_selected
        self._update_translation_text("เลือกพื้นที่ที่จะใช้แสดงคำแปล (overlay)")
        self._start_region_selection()

    def _on_overlay_area_selected(self, bbox):
        """Handle the second selection (where to show translated overlay)."""
        if self.watch_bbox is None:
            logger.warning("Overlay area selected but watch_bbox is missing")
            return

        logger.info("Overlay area selected: %s", bbox)
        self.after_selection_action = None
        self.overlay_bbox = bbox
        self._save_watch_positions()
        self._create_overlay_window()
        self._update_overlay_text("รอข้อความจาก watch ...")
        self._start_watch_after_selection(self.watch_bbox)

    def _on_hotkey_stop_watch(self):
        """Stop watch mode."""
        logger.info("Hotkey stop watch pressed")
        self._stop_watch()

    # ------------------------------------------------------------------
    # Screen region selection
    # ------------------------------------------------------------------
    def _start_region_selection(self):
        """Show fullscreen transparent overlay to select region."""
        if self.selection_window is not None:
            return

        self.selection_window = tk.Toplevel(self.root)
        self.selection_window.attributes("-fullscreen", True)
        self.selection_window.attributes("-topmost", True)
        self.selection_window.attributes("-alpha", 0.25)
        self.selection_window.configure(bg="black")

        self.selection_canvas = tk.Canvas(
            self.selection_window,
            cursor="cross",
            bg="black",
            highlightthickness=0,
        )
        self.selection_canvas.pack(fill=tk.BOTH, expand=True)

        self.selection_canvas.bind("<ButtonPress-1>", self._on_mouse_press)
        self.selection_canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.selection_canvas.bind("<ButtonRelease-1>", self._on_mouse_release)

        logger.info("Selection overlay shown")

    def _on_mouse_press(self, event):
        """Start selection at mouse down."""
        self.sel_start_x = event.x_root
        self.sel_start_y = event.y_root

        if self.sel_rect is not None:
            self.selection_canvas.delete(self.sel_rect)
            self.sel_rect = None

    def _on_mouse_drag(self, event):
        """Update rectangle while dragging."""
        if self.sel_start_x is None or self.sel_start_y is None:
            return

        cur_x = event.x_root
        cur_y = event.y_root

        canvas_x1 = self.sel_start_x - self.selection_window.winfo_rootx()
        canvas_y1 = self.sel_start_y - self.selection_window.winfo_rooty()
        canvas_x2 = cur_x - self.selection_window.winfo_rootx()
        canvas_y2 = cur_y - self.selection_window.winfo_rooty()

        if self.sel_rect is not None:
            self.selection_canvas.delete(self.sel_rect)

        self.sel_rect = self.selection_canvas.create_rectangle(
            canvas_x1,
            canvas_y1,
            canvas_x2,
            canvas_y2,
            outline="red",
            width=2,
        )

    def _on_mouse_release(self, event):
        """Finish selection and call the stored callback."""
        if self.sel_start_x is None or self.sel_start_y is None:
            self._destroy_selection_window()
            return

        end_x = event.x_root
        end_y = event.y_root

        x1 = min(self.sel_start_x, end_x)
        y1 = min(self.sel_start_y, end_y)
        x2 = max(self.sel_start_x, end_x)
        y2 = max(self.sel_start_y, end_y)

        logger.info("Selected region: (%d, %d, %d, %d)", x1, y1, x2, y2)

        self._destroy_selection_window()

        if abs(x2 - x1) < 5 or abs(y2 - y1) < 5:
            logger.info("Selection too small, ignored")
            return

        bbox = (x1, y1, x2, y2)

        if self.after_selection_action is not None:
            self.after_selection_action(bbox)

    def _destroy_selection_window(self):
        """Destroy overlay window and reset selection state."""
        if self.selection_window is not None:
            self.selection_window.destroy()

        self.selection_window = None
        self.selection_canvas = None
        self.sel_rect = None
        self.sel_start_x = None
        self.sel_start_y = None

    # ------------------------------------------------------------------
    # Capture / OCR / Translate
    # ------------------------------------------------------------------
    def _process_single_capture(self, bbox):
        """Run one-shot capture and process in background."""
        threading.Thread(
            target=self._capture_and_process_region,
            args=(bbox,),
            daemon=True,
        ).start()

    def _capture_and_process_region(self, bbox):
        """Capture region, OCR English, translate to Thai, update UI."""
        try:
            logger.info("Capturing region (single shot)...")
            image = ImageGrab.grab(bbox=bbox)

            logger.info("Running OCR (single shot)...")
            text_en = pytesseract.image_to_string(image, lang="eng").strip()
            logger.info("OCR result length: %d", len(text_en))

            self.root.after(0, self._update_original_text, text_en)

            if not text_en:
                logger.info("No text detected in single capture.")
                return

            logger.info("Translating to Thai (single shot)...")
            translator = GoogleTranslator(source="en", target="th")
            text_th = translator.translate(text_en).strip()
            logger.info("Translation done (single shot).")

            self.root.after(0, self._update_translation_text, text_th)

        except Exception as e:
            logger.exception("Error during single capture: %s", e)

            # Use inner function with default arg to avoid free variable bug
            def show_err(msg=str(e)):
                messagebox.showerror("Error", f"An error occurred:\n{msg}")

            self.root.after(0, show_err)

    # ------------------------------------------------------------------
    # Font size helpers + UI update
    # ------------------------------------------------------------------
    def _auto_font_size(self, text, min_size=12, max_size=28):
        """
        Calculate font size based on text length.
        Short text = bigger font, long text = smaller font.
        """
        length = len(text)
        if length == 0:
            return max_size

        # Simple scale: decrease by 1 point per 10 chars
        size = int(max_size - (length / 10))
        size = max(min_size, min(max_size, size))
        return size

    def _update_original_text(self, text_en: str):
        """Update top text box and adjust font size."""
        font_size = self._auto_font_size(text_en, min_size=12, max_size=26)
        self.text_original.config(font=("Arial", font_size))
        self.text_original.delete("1.0", tk.END)
        self.text_original.insert(tk.END, text_en)

    def _reset_translation_history(self):
        """Clear translation panel (used before starting a new watch history)."""
        if self.text_translation is not None:
            self.text_translation.delete("1.0", tk.END)
        self.last_history_timestamp = None

    def _update_translation_text(self, text_th: str, append: bool = False):
        """Update translation panel with optional history append behaviour."""
        font_size = self._auto_font_size(text_th, min_size=12, max_size=24)
        self.text_translation.config(font=("Arial", font_size))

        if append:
            clean = text_th.strip()
            if not clean:
                return
            if self.text_translation.index("end-1c") != "1.0":
                self.text_translation.insert(tk.END, "\n\n")
            self.text_translation.insert(tk.END, clean)
        else:
            self.text_translation.delete("1.0", tk.END)
            self.text_translation.insert(tk.END, text_th)

        self.text_translation.see(tk.END)

    def _append_watch_history(self, text_th: str):
        """Append the latest watch translation and auto-scroll."""
        if not self.watch_mode_active:
            self._update_translation_text(text_th)
            return

        now = monotonic()
        if (
            self.last_history_timestamp is not None
            and (now - self.last_history_timestamp) >= self.history_reset_interval
        ):
            self._reset_translation_history()

        self._update_translation_text(text_th, append=True)
        self.last_history_timestamp = now

    # ------------------------------------------------------------------
    # Watch-mode helpers
    # ------------------------------------------------------------------
    def _looks_like_english(self, text: str, threshold: float = 0.3) -> bool:
        """
        Return True if most alphabetic characters are English (a-z).
        Used to skip screens that are mostly non-English.
        """
        letters = [c for c in text if c.isalpha()]
        if not letters:
            return False

        english_letters = [c for c in letters if "a" <= c.lower() <= "z"]
        ratio = len(english_letters) / len(letters)
        return ratio >= threshold

    def _start_watch_after_selection(self, bbox):
        """Start watching the selected region."""
        logger.info("Starting watch mode for bbox: %s", bbox)

        self._reset_translation_history()
        self.watch_mode_active = True
        self.watch_bbox = bbox
        self.last_watch_text = ""
        self.watch_stop_event = threading.Event()

        self.watch_thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
        )
        self.watch_thread.start()

    def _stop_watch(self, destroy_overlay=True):
        """Stop watch loop if running."""
        if self.watch_thread is not None:
            logger.info("Stopping watch mode...")
            self.watch_stop_event.set()
            self.watch_thread = None
            self.last_watch_text = ""
            self.watch_mode_active = False
            self.last_history_timestamp = None

        if destroy_overlay:
            self._destroy_overlay_window()
            self.overlay_bbox = None

    def _watch_loop(self):
        """
        Periodically capture watch_bbox, OCR and translate when text changes.
        """
        if self.watch_bbox is None:
            return

        logger.info("Watch loop started.")
        interval_sec = 2.0  # adjust to reduce / increase CPU usage

        while not self.watch_stop_event.is_set():
            try:
                image = ImageGrab.grab(bbox=self.watch_bbox)
                text_en = pytesseract.image_to_string(image, lang="eng").strip()

                if not text_en:
                    # no text, do nothing
                    pass
                elif (text_en != self.last_watch_text) and self._looks_like_english(text_en):
                    logger.info("Watch: new English text detected (len=%d)", len(text_en))
                    self.last_watch_text = text_en

                    # update UI
                    self.root.after(0, self._update_original_text, text_en)

                    translator = GoogleTranslator(source="en", target="th")
                    text_th = translator.translate(text_en).strip()
                    self.root.after(0, self._append_watch_history, text_th)
                    self.root.after(0, self._update_overlay_text, text_th)

            except Exception as e:
                logger.exception("Error in watch loop: %s", e)

            # wait for interval or stop
            self.watch_stop_event.wait(interval_sec)

        logger.info("Watch loop stopped.")

    # ------------------------------------------------------------------
    # Overlay helpers
    # ------------------------------------------------------------------
    def _create_overlay_window(self):
        """Create or recreate overlay window inside the chosen bbox."""
        if self.overlay_bbox is None:
            return

        self._destroy_overlay_window()

        x1, y1, x2, y2 = self.overlay_bbox
        width = max(120, x2 - x1)
        height = max(60, y2 - y1)

        self.overlay_window = tk.Toplevel(self.root)
        self.overlay_window.overrideredirect(True)
        self.overlay_window.attributes("-topmost", True)
        self.overlay_window.attributes("-alpha", 0.9)
        self.overlay_window.configure(bg="#111111")
        self.overlay_window.geometry(f"{width}x{height}+{x1}+{y1}")

        wrap_len = max(50, width - 20)
        self.overlay_label = tk.Label(
            self.overlay_window,
            text="",
            fg="#ffffff",
            bg="#111111",
            justify="left",
            anchor="nw",
            wraplength=wrap_len,
        )
        self.overlay_label.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def _destroy_overlay_window(self):
        """Destroy overlay window if it exists."""
        if self.overlay_window is not None:
            self.overlay_window.destroy()

        self.overlay_window = None
        self.overlay_label = None

    def _update_overlay_text(self, text_th: str):
        """Update overlay label text if overlay mode is active."""
        if self.overlay_label is None:
            return

        font_size = self._auto_font_size(text_th, min_size=14, max_size=32)
        self.overlay_label.config(
            text=text_th,
            font=("Leelawadee UI", font_size, "bold"),
        )

    # ------------------------------------------------------------------
    # Close / main loop
    # ------------------------------------------------------------------
    def _on_close(self):
        """Cleanly stop watch thread and close window."""
        self._stop_watch()
        self.root.destroy()

    def run(self):
        """Start Tkinter main loop."""
        logger.info("Starting OCR Translator App")
        self.root.mainloop()


if __name__ == "__main__":
    app = OcrTranslatorApp()
    app.run()
