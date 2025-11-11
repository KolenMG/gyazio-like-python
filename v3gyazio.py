"""
Kolen'sSnapshots
A combined client + server Python program that provides GIF recording, screenshots,
selectable recording rectangle overlay, pause/resume/stop buttons, configurable FPS,
auto-upload to a server and gallery viewing.

This single file supports two modes:
  1) server: runs a Flask server that accepts GIF uploads and serves a gallery page
     Usage: python kolens_snapshots.py server --host 0.0.0.0 --port 5000

  2) client: runs a PyQt5 desktop app for recording and screenshotting
     Usage: python kolens_snapshots.py client --server-url http://localhost:5000

Requirements (install with pip):
  pip install flask pyqt5 mss imageio pillow requests numpy

Notes / Features implemented:
  - Selectable rectangular area (click "Select Area" then drag on screen)
  - While recording, an always-on-top translucent overlay shows the rectangle
  - Start / Pause/Resume / Stop recording controls
  - Choose FPS from dropdown
  - Screenshot button (saves locally and also uploads if server provided)
  - GIF frames are saved as PNGs to a temp folder during capture then assembled
    into an optimized GIF using imageio.mimsave (reduces memory use)
  - After upload, the returned URL is copied to the clipboard and opened in the
    default browser automatically
  - Server stores uploads in /uploads and shows a gallery page with a reserved
    area for future "ads" (simply drop files into /ads to have them shown on page)

Caveats / further improvements you may want later:
  - Authentication for your server (not included)
  - Rate limiting and content scanning on uploads
  - Finer GIF optimization (use gifsicle or ffmpeg on the server)
  - Windows vs macOS specialty tweaks for transparent overlay input handling

"""

import sys
import os
import argparse
import threading
import time
import uuid
import json
import tempfile
import shutil
import webbrowser
from pathlib import Path

# Server-side imports
from flask import Flask, request, jsonify, send_from_directory, render_template_string

# Client GUI and capture imports
from PyQt5 import QtCore, QtGui, QtWidgets
import mss
from PIL import Image
import imageio
import requests
import numpy as np

# --------------------------- Server code ---------------------------
GALLERY_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Kolen'sSnapshots - Gallery</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    header { display:flex; justify-content:space-between; align-items:center; }
    .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap:12px; margin-top:16px; }
    .card { border:1px solid #ddd; padding:8px; border-radius:6px; background:#fafafa; }
    .card img { max-width:100%; height:auto; display:block; }
    .meta { font-size:12px; margin-top:6px; color:#333; }
    .ads { margin-top:20px; border:2px dashed #bbb; padding:12px; border-radius:6px; }
  </style>
</head>
<body>
  <header>
    <h1>Kolen'sSnapshots — Gallery</h1>
    <div>
      <a href="/upload_form">Upload (web)</a>
    </div>
  </header>

  <section class="ads">
    <h3>Ads / Custom area (place your images/videos/gifs into the server's /ads folder)</h3>
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
      {% for a in ads %}
        <div style="width:140px;">
          <img src="/ads/{{ a }}" style="max-width:100%;" />
        </div>
      {% endfor %}
      {% if ads|length == 0 %}
        <div style="color:#666;">(no ads found — drop images/gifs into the server 'ads' directory)</div>
      {% endif %}
    </div>
  </section>

  <section>
    <h2>Recent Uploads</h2>
    <div class="grid">
      {% for item in items %}
        <div class="card">
          <a href="{{ item.url }}" target="_blank"><img src="{{ item.url }}" /></a>
          <div class="meta">{{ item.name }} — {{ item.time }}</div>
        </div>
      {% endfor %}
    </div>
  </section>
</body>
</html>
"""

UPLOAD_FORM = """
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Upload</title></head>
  <body>
    <h2>Upload a file</h2>
    <form method="post" enctype="multipart/form-data" action="/upload">
      <input type="file" name="file" />
      <input type="submit" value="Upload" />
    </form>
    <p><a href="/gallery">Back to gallery</a></p>
  </body>
</html>
"""


def create_server(host='0.0.0.0', port=5000, storage_dir='server_data'):
    app = Flask(__name__)
    base = Path(storage_dir)
    uploads_dir = base / 'uploads'
    ads_dir = base / 'ads'
    meta_file = base / 'meta.json'
    uploads_dir.mkdir(parents=True, exist_ok=True)
    ads_dir.mkdir(parents=True, exist_ok=True)

    if not meta_file.exists():
        meta_file.write_text(json.dumps([]))

    def read_meta():
        return json.loads(meta_file.read_text())

    def write_meta(data):
        meta_file.write_text(json.dumps(data, indent=2))

    @app.route('/')
    def index():
        return render_template_string(GALLERY_TEMPLATE, items=reversed(read_meta()), ads=os.listdir(ads_dir))

    @app.route('/gallery')
    def gallery():
        return index()

    @app.route('/upload_form')
    def upload_form():
        return UPLOAD_FORM

    @app.route('/uploads/<path:filename>')
    def uploaded_file(filename):
        return send_from_directory(uploads_dir, filename)

    @app.route('/ads/<path:filename>')
    def ads_file(filename):
        return send_from_directory(ads_dir, filename)

    @app.route('/upload', methods=['POST'])
    def upload():
        f = request.files.get('file')
        if not f:
            return jsonify({'error':'no file'}), 400
        ext = Path(f.filename).suffix
        uid = str(uuid.uuid4())
        filename = f'{uid}{ext}'
        out = uploads_dir / filename
        f.save(out)
        meta = read_meta()
        item = {'id': uid, 'name': f.filename, 'filename': filename, 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'url': f'/uploads/{filename}'}
        meta.append(item)
        write_meta(meta)
        # return full absolute URL
        base_url = request.host_url.rstrip('/')
        return jsonify({'url': base_url + item['url']})

    def run():
        print(f"Starting server on {host}:{port} — data dir: {base.resolve()}")
        app.run(host=host, port=port)

    return run

# --------------------------- Client code ---------------------------

class ImageLabel(QtWidgets.QLabel):
    """
    QLabel that shows a pixmap and allows click-drag selection.
    It draws overlays in its own paintEvent (no modification of the pixmap),
    avoiding QPaintDevice-destroyed errors.
    Emits rectSelected when a selection is finished (QRect in image coords).
    """
    rectSelected = QtCore.pyqtSignal(QtCore.QRect)

    def __init__(self, pixmap: QtGui.QPixmap, parent=None):
        super().__init__(parent)
        self.base_pixmap = pixmap
        self.start = None         # QPoint in widget coords
        self.end = None           # QPoint in widget coords
        self.selected = None      # QRect in image coords
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        # Draw the base pixmap scaled to the label rect (keeps it simple on multi-monitor)
        painter.drawPixmap(self.rect(), self.base_pixmap)

        # If we have an in-progress drag, draw it in widget coords
        if self.start is not None and self.end is not None:
            pen = QtGui.QPen(QtGui.QColor(0, 180, 255), 3)
            painter.setPen(pen)
            r = QtCore.QRect(self.start, self.end).normalized()
            painter.drawRect(r)
        # Else if we have a finished selection in image coords, draw it scaled to widget
        elif self.selected is not None:
            pen = QtGui.QPen(QtGui.QColor(255, 0, 0), 3)
            painter.setPen(pen)
            sx = self.width() / max(1, self.base_pixmap.width())
            sy = self.height() / max(1, self.base_pixmap.height())
            r = QtCore.QRect(int(self.selected.x() * sx),
                             int(self.selected.y() * sy),
                             int(self.selected.width() * sx),
                             int(self.selected.height() * sy))
            painter.drawRect(r)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.start = event.pos()
            self.end = self.start
            self.selected = None
            self.update()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            self.end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.start is not None:
            self.end = event.pos()
            r_widget = QtCore.QRect(self.start, self.end).normalized()
            # Map widget coords to image coords
            sx = self.base_pixmap.width() / max(1, self.width())
            sy = self.base_pixmap.height() / max(1, self.height())
            mapped = QtCore.QRect(int(r_widget.x() * sx),
                                  int(r_widget.y() * sy),
                                  int(r_widget.width() * sx),
                                  int(r_widget.height() * sy))
            # Clip to image bounds
            mapped = mapped.intersected(QtCore.QRect(0, 0, self.base_pixmap.width(), self.base_pixmap.height()))
            self.selected = mapped
            # clear transient drag coords
            self.start = None
            self.end = None
            self.update()
            # emit signal so caller can react if desired
            self.rectSelected.emit(self.selected)

class ScreenshotSelector(QtWidgets.QDialog):
    """
    Modal dialog that shows a screenshot of the virtual screen and lets the user
    draw a rectangle. Returns a QRect in screen/image coordinates or None if cancelled.
    """
    def __init__(self, pixmap, screen_size, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Select Area')
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        self.screen_size = screen_size  # (w,h)

        # Use ImageLabel for all drawing to avoid modifying QPixmap while Qt paints it.
        self.label = ImageLabel(pixmap, parent=self)
        self.label.rectSelected.connect(self._on_rect_selected)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(btns)
        self.setLayout(layout)

        # Keep selected rectangle in image coords
        self.selected = None

        # Try to size reasonably; user can resize if needed
        self.resize(min(1200, pixmap.width()), min(800, pixmap.height()))

    @staticmethod
    def get_selection(parent=None):
        sct = mss.mss()
        # monitor 0 is the virtual screen (all monitors combined)
        m = sct.monitors[0]
        img = sct.grab(m)
        pil = Image.frombytes('RGB', img.size, img.rgb)
        w, h = img.size
        # convert to QPixmap
        data = pil.tobytes('raw', 'RGB')
        qim = QtGui.QImage(data, w, h, QtGui.QImage.Format_RGB888)
        pix = QtGui.QPixmap.fromImage(qim)

        dlg = ScreenshotSelector(pix, (w, h), parent=parent)
        res = dlg.exec_()
        if res == QtWidgets.QDialog.Accepted and dlg.selected:
            return dlg.selected
        return None

    def _on_rect_selected(self, rect):
        # rect is already in image/screen coords
        self.selected = rect



# keep the IndicatorOverlay as-is (no change)
class IndicatorOverlay(QtWidgets.QWidget):
    """Small always-on-top transparent window that draws the selected recording rectangle
    so the user sees which area is being recorded while using other apps.
    """
    def __init__(self, rect=None):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.rect = rect
        self.hide()

    def set_rect(self, rect):
        self.rect = rect
        if rect is None:
            self.hide()
            return
        self.setGeometry(rect)
        self.show()

    def paintEvent(self, event):
        if not self.rect:
            return
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing)
        qp.setPen(QtGui.QPen(QtGui.QColor(255,0,0), 3))
        qp.drawRect(0,0,self.width()-1, self.height()-1)


    def show(self):
        # ensure full-screen mode and focus so mouse events are received reliably
        super().show()
        try:
            self.showFullScreen()
        except Exception:
            # fallback if showFullScreen is not allowed in some environments
            self.setWindowState(self.windowState() | QtCore.Qt.WindowFullScreen)
        self.raise_()
        self.activateWindow()

    def paintEvent(self, event):
        if self.begin and self.end:
            qp = QtGui.QPainter(self)
            qp.setPen(QtGui.QPen(QtGui.QColor(0, 180, 255), 2))
            brush = QtGui.QBrush(QtGui.QColor(0,0,0,100))
            qp.fillRect(self.rect(), brush)
            # clear selection area
            r = QtCore.QRect(self.begin, self.end).normalized()
            qp.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
            qp.fillRect(r, QtGui.QBrush())
            qp.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            qp.setPen(QtGui.QPen(QtGui.QColor(0, 180, 255), 3))
            qp.drawRect(r)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.begin = event.pos()
            self.end = self.begin
            self.update()

    def mouseMoveEvent(self, event):
        if self.begin is not None:
            self.end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.begin is not None:
            self.end = event.pos()
            self.rect = QtCore.QRect(self.begin, self.end).normalized()
        else:
            self.rect = None
        self.hide()

    def keyPressEvent(self, event):
        # allow ESC to cancel selection
        if event.key() == QtCore.Qt.Key_Escape:
            self.rect = None
            self.hide()
        else:
            super().keyPressEvent(event)


    def paintEvent(self, event):
        if self.begin and self.end:
            qp = QtGui.QPainter(self)
            qp.setPen(QtGui.QPen(QtGui.QColor(0, 180, 255), 2))
            brush = QtGui.QBrush(QtGui.QColor(0,0,0,100))
            qp.fillRect(self.rect(), brush)
            # clear selection area
            r = QtCore.QRect(self.begin, self.end).normalized()
            qp.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
            qp.fillRect(r, QtGui.QBrush())
            qp.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            qp.setPen(QtGui.QPen(QtGui.QColor(0, 180, 255), 3))
            qp.drawRect(r)

    def mousePressEvent(self, event):
        self.begin = event.pos()
        self.end = self.begin
        self.update()

    def mouseMoveEvent(self, event):
        self.end = event.pos()
        self.update()

    def mouseReleaseEvent(self, event):
        self.end = event.pos()
        self.rect = QtCore.QRect(self.begin, self.end).normalized()
        self.hide()


class IndicatorOverlay(QtWidgets.QWidget):
    """Small always-on-top transparent window that draws the selected recording rectangle
    so the user sees which area is being recorded while using other apps.
    """
    def __init__(self, rect=None):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.rect = rect
        self.hide()

    def set_rect(self, rect):
        self.rect = rect
        if rect is None:
            self.hide()
            return
        self.setGeometry(rect)
        self.show()

    def paintEvent(self, event):
        if not self.rect:
            return
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing)
        qp.setPen(QtGui.QPen(QtGui.QColor(255,0,0), 3))
        qp.drawRect(0,0,self.width()-1, self.height()-1)


class RecorderThread(QtCore.QThread):
    finished_signal = QtCore.pyqtSignal(str)
    progress_signal = QtCore.pyqtSignal(str)

    def __init__(self, bbox, fps, server_url=None):
        super().__init__()
        self.bbox = bbox  # (left, top, width, height)
        self.fps = fps
        self.server_url = server_url
        self._pause = False
        self._stop = False
        self.tmpdir = Path(tempfile.mkdtemp(prefix='kolen_frames_'))

    def run(self):
        sct = mss.mss()
        frame_interval = 1.0 / self.fps
        frame_paths = []
        self.progress_signal.emit('recording')
        frame_num = 0
        try:
            while not self._stop:
                if self._pause:
                    time.sleep(0.05)
                    continue
                t0 = time.time()
                monitor = {
                    'left': self.bbox[0],
                    'top': self.bbox[1],
                    'width': self.bbox[2],
                    'height': self.bbox[3]
                }
                s = sct.grab(monitor)
                img = Image.frombytes('RGB', s.size, s.rgb)
                frame_path = self.tmpdir / f'frame_{frame_num:06d}.png'
                img.save(frame_path)
                frame_paths.append(str(frame_path))
                frame_num += 1
                elapsed = time.time() - t0
                to_sleep = frame_interval - elapsed
                if to_sleep > 0:
                    time.sleep(to_sleep)
        except Exception as e:
            self.progress_signal.emit('error: ' + str(e))
            self._stop = True

        # no frames captured
        if len(frame_paths) == 0:
            self.progress_signal.emit('no_frames')
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.finished_signal.emit('')
            return

        out_gif = self.tmpdir / f'recording_{int(time.time())}.gif'
        try:
            self.progress_signal.emit('assembling')
            imageio.mimsave(str(out_gif), [imageio.v2.imread(p) for p in frame_paths], fps=self.fps)
        except Exception as e:
            self.progress_signal.emit('error: ' + str(e))
            self.finished_signal.emit('')
            return

        # Attempt upload if server URL provided
        url = ''
        if self.server_url:
            self.progress_signal.emit('uploading')
            try:
                with open(out_gif, 'rb') as f:
                    files = {'file': f}
                    r = requests.post(self.server_url.rstrip('/') + '/upload', files=files, timeout=30)
                if r.status_code == 200:
                    url = r.json().get('url', '') or ''
                else:
                    url = ''
            except Exception as e:
                # upload failed; keep local path
                url = ''
        else:
            url = str(out_gif)

        # cleanup individual frame files (we keep the final GIF)
        for p in frame_paths:
            try:
                os.remove(p)
            except Exception:
                pass

        # emit the final path or uploaded URL
        self.finished_signal.emit(url if url else str(out_gif))


    def pause(self):
        self._pause = True

    def resume(self):
        self._pause = False

    def stop(self):
        self._stop = True


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kolen'sSnapshots")
        self.resize(420, 220)
        layout = QtWidgets.QVBoxLayout(self)

        row1 = QtWidgets.QHBoxLayout()
        self.select_area_btn = QtWidgets.QPushButton('Select Area')
        self.start_btn = QtWidgets.QPushButton('Start')
        self.pause_btn = QtWidgets.QPushButton('Pause')
        self.stop_btn = QtWidgets.QPushButton('Stop')
        row1.addWidget(self.select_area_btn)
        row1.addWidget(self.start_btn)
        row1.addWidget(self.pause_btn)
        row1.addWidget(self.stop_btn)
        layout.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        self.screenshot_btn = QtWidgets.QPushButton('Screenshot')
        row2.addWidget(self.screenshot_btn)
        row2.addStretch()
        row2.addWidget(QtWidgets.QLabel('FPS:'))
        self.fps_combo = QtWidgets.QComboBox()
        self.fps_combo.addItems(['5','10','12','15','20','24','30'])
        self.fps_combo.setCurrentText('12')
        row2.addWidget(self.fps_combo)
        layout.addLayout(row2)

        row3 = QtWidgets.QHBoxLayout()
        row3.addWidget(QtWidgets.QLabel('Server URL (leave empty to skip upload):'))
        self.server_edit = QtWidgets.QLineEdit('http://localhost:5000')
        row3.addWidget(self.server_edit)
        layout.addLayout(row3)

        self.status = QtWidgets.QLabel('Ready')
        layout.addWidget(self.status)

        # state
        # state
        self.selected_rect = None  # QRect in screen coordinates
        self.indicator = IndicatorOverlay()
        # old SelectionOverlay removed — we use the ScreenshotSelector dialog instead
        self.overlay = None
        self.recorder = None

        # signals
        self.select_area_btn.clicked.connect(self.on_select_area)
        self.start_btn.clicked.connect(self.on_start)
        self.pause_btn.clicked.connect(self.on_pause)
        self.stop_btn.clicked.connect(self.on_stop)
        self.screenshot_btn.clicked.connect(self.on_screenshot)

        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

    def on_select_area(self):
        # Use the screenshot-based selector (more reliable across OSes)
        self.status.setText('Capturing screen for selection...')
        QtWidgets.QApplication.processEvents()
        rect = ScreenshotSelector.get_selection(self)
        if rect:
            # rect is already in screen coordinates
            self.selected_rect = rect
            self.indicator.set_rect(self.selected_rect)
            self.status.setText(f'Selected area: {rect.x()},{rect.y()} {rect.width()}x{rect.height()}')
        else:
            self.status.setText('Selection cancelled')

    def _wait_for_selection(self):
        # kept for backward compatibility but not used by the screenshot selector
        pass


        # poll (small) — acceptable here to keep code simple
        def poll():
            while self.overlay.isVisible():
                time.sleep(0.05)
            # selection done
            if self.overlay.rect:
                r = self.overlay.rect
                # map to global (already global because overlay is full-screen)
                self.selected_rect = r
                self.indicator.set_rect(self.selected_rect)
                self.status.setText(f'Selected area: {r.x()},{r.y()} {r.width()}x{r.height()}')
            else:
                self.status.setText('Selection cancelled')
        t = threading.Thread(target=poll, daemon=True)
        t.start()

    def on_start(self):
        if not self.selected_rect:
            self.status.setText('Please select an area first')
            return
        if self.recorder and self.recorder.isRunning():
            self.status.setText('Already recording')
            return
        bbox = (self.selected_rect.x(), self.selected_rect.y(), self.selected_rect.width(), self.selected_rect.height())
        fps = int(self.fps_combo.currentText())
        server_url = self.server_edit.text().strip() or None
        self.recorder = RecorderThread(bbox, fps, server_url)
        self.recorder.finished_signal.connect(self.on_record_finished)
        self.recorder.progress_signal.connect(self.on_record_progress)
        self.recorder.start()
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self.select_area_btn.setEnabled(False)
        self.status.setText('Recording...')

    def on_pause(self):
        if not self.recorder:
            return
        if not self.recorder._pause:
            self.recorder.pause()
            self.pause_btn.setText('Resume')
            self.status.setText('Paused')
        else:
            self.recorder.resume()
            self.pause_btn.setText('Pause')
            self.status.setText('Recording...')

    def on_stop(self):
        if not self.recorder:
            return
        self.recorder.stop()
        self.status.setText('Stopping — assembling GIF...')
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

    def on_screenshot(self):
        if not self.selected_rect:
            self.status.setText('Select an area first')
            return
        r = self.selected_rect
        monitor = {'left': r.x(), 'top': r.y(), 'width': r.width(), 'height': r.height()}
        sct = mss.mss()
        s = sct.grab(monitor)
        img = Image.frombytes('RGB', s.size, s.rgb)
        out = Path.cwd() / f'screenshot_{int(time.time())}.png'
        img.save(out)
        self.status.setText(f'Screenshot saved: {out.name}')
        # optionally upload
        server_url = self.server_edit.text().strip()
        if server_url:
            try:
                files = {'file': open(out, 'rb')}
                r = requests.post(server_url.rstrip('/') + '/upload', files=files)
                if r.status_code == 200:
                    url = r.json().get('url')
                    QtWidgets.QApplication.clipboard().setText(url)
                    webbrowser.open(url)
                    self.status.setText('Screenshot uploaded and opened in browser')
                else:
                    self.status.setText('Upload failed')
            except Exception as e:
                self.status.setText('Upload error: ' + str(e))

    @QtCore.pyqtSlot(str)
    def on_record_progress(self, text):
        self.status.setText(text)

    @QtCore.pyqtSlot(str)
    def on_record_finished(self, path_or_url):
        self.status.setText('Done: ' + (path_or_url or '(no url)'))
        # copy to clipboard and open browser if looks like url
        if path_or_url.startswith('http://') or path_or_url.startswith('https://'):
            QtWidgets.QApplication.clipboard().setText(path_or_url)
            webbrowser.open(path_or_url)
            self.status.setText('Uploaded. URL copied and opened in browser')
        else:
            self.status.setText('Saved locally: ' + path_or_url)
        # reset buttons
        self.start_btn.setEnabled(True)
        self.pause_btn.setText('Pause')
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.select_area_btn.setEnabled(True)


# --------------------------- Entrypoint ---------------------------

def main():
    parser = argparse.ArgumentParser(description="Kolen'sSnapshots — recorder + server")
    sub = parser.add_subparsers(dest='mode', required=True)
    p_server = sub.add_parser('server')
    p_server.add_argument('--host', default='0.0.0.0')
    p_server.add_argument('--port', type=int, default=5000)
    p_server.add_argument('--data', default='server_data')

    p_client = sub.add_parser('client')
    p_client.add_argument('--server-url', default='http://localhost:5000')

    args = parser.parse_args()

    if args.mode == 'server':
        run_server = create_server(host=args.host, port=args.port, storage_dir=args.data)
        run_server()
        return

    if args.mode == 'client':
        app = QtWidgets.QApplication(sys.argv)
        w = MainWindow()
        w.server_edit.setText(args.server_url)
        w.show()
        sys.exit(app.exec_())


if __name__ == '__main__':
    main()
