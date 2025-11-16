import logging
import threading
import tkinter as tk
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

        # ---- Build UI & hotkeys ----
        self._build_ui()
        self._register_hotkey()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        """Create UI: top and bottom text boxes share window 50/50."""
        hotkey_label = tk.Label(
            self.root,
            text="Hotkeys: Ctrl+Shift+Q = แปลครั้งเดียว | Ctrl+Shift+W = เฝ้าพื้นที่ | Ctrl+Shift+E = หยุดเฝ้า",
            bg="#f5f5f5",
            fg="#333333",
        )
        hotkey_label.pack(pady=5)

        # central frame that will be split into two rows (top/bottom)
        center_frame = tk.Frame(self.root, bg="#f5f5f5")
        center_frame.pack(fill=tk.BOTH, expand=True)

        center_frame.rowconfigure(0, weight=1)
        center_frame.rowconfigure(1, weight=1)
        center_frame.columnconfigure(0, weight=1)

        # -------- Top (OCR) --------
        top_frame = tk.Frame(center_frame, bg="#f5f5f5")
        top_frame.grid(row=0, column=0, sticky="nsew")

        top_label = tk.Label(top_frame, text="Original (OCR EN)", bg="#f5f5f5")
        top_label.pack(anchor="w", padx=10)

        self.text_original = scrolledtext.ScrolledText(
            top_frame,
            wrap=tk.WORD,   # auto wrap lines
        )
        self.text_original.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))

        # -------- Bottom (Translation) --------
        bottom_frame = tk.Frame(center_frame, bg="#f5f5f5")
        bottom_frame.grid(row=1, column=0, sticky="nsew")

        bottom_label = tk.Label(bottom_frame, text="Translation (TH)", bg="#f5f5f5")
        bottom_label.pack(anchor="w", padx=10)

        self.text_translation = scrolledtext.ScrolledText(
            bottom_frame,
            wrap=tk.WORD,
        )
        self.text_translation.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------
    def _register_hotkey(self):
        """Register global hotkeys for capture and watch."""
        keyboard.add_hotkey("ctrl+shift+q", self._on_hotkey_single)
        keyboard.add_hotkey("ctrl+shift+w", self._on_hotkey_watch)
        keyboard.add_hotkey("ctrl+shift+e", self._on_hotkey_stop_watch)
        logger.info("Hotkeys registered: Q(single), W(watch), E(stop watch)")

    def _on_hotkey_single(self):
        """Single capture mode."""
        logger.info("Hotkey single capture pressed")
        self.after_selection_action = self._process_single_capture
        self.root.after(0, self._start_region_selection)

    def _on_hotkey_watch(self):
        """Start region selection for watch mode."""
        logger.info("Hotkey watch pressed")
        self.after_selection_action = self._start_watch_after_selection
        self.root.after(0, self._start_region_selection)

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

    def _update_translation_text(self, text_th: str):
        """Update bottom text box and adjust font size."""
        font_size = self._auto_font_size(text_th, min_size=12, max_size=24)
        self.text_translation.config(font=("Arial", font_size))
        self.text_translation.delete("1.0", tk.END)
        self.text_translation.insert(tk.END, text_th)

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
        self._stop_watch()  # stop existing one if any

        self.watch_bbox = bbox
        self.last_watch_text = ""
        self.watch_stop_event = threading.Event()

        self.watch_thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
        )
        self.watch_thread.start()

    def _stop_watch(self):
        """Stop watch loop if running."""
        if self.watch_thread is not None:
            logger.info("Stopping watch mode...")
            self.watch_stop_event.set()
            self.watch_thread = None

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
                    self.root.after(0, self._update_translation_text, text_th)

            except Exception as e:
                logger.exception("Error in watch loop: %s", e)

            # wait for interval or stop
            self.watch_stop_event.wait(interval_sec)

        logger.info("Watch loop stopped.")

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
