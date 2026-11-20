# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6-Essentials>=6.8,<7"]
# ///
"""运行 uv run sony_raw_gallery.py。将 sony_camera.py 放在同一目录。"""
import threading
import time
from pathlib import Path
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer, QSize, QSettings, QSignalBlocker, QUrl
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
    QSpinBox, QProgressBar, QDialog,
)

if __package__:
    from . import sony_camera as backend
else:
    import sony_camera as backend


class Gallery(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Sony RAW 照片浏览与下载')
        self.resize(1000, 760)
        self.settings = QSettings('SonyRawTools', 'Gallery')
        self.cancel_event = threading.Event()
        self.camera = None
        self.connection_identity = None
        self.items_by_handle = {}
        self.selected_count = 0
        self.selected_size = 0
        self.thumbnails_supported = True
        self.events = queue.Queue()
        # 同一会话所有操作按顺序在一个后台线程运行。
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.busy = False
        self.close_pending = False
        self.output = Path(__file__).resolve().parent / 'SonyPhotos'
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.ip = QLineEdit()
        self.ip.setText(self.settings.value('ip', ''))
        self.ip.setPlaceholderText('相机 IP（留空则自动发现）')
        self.guid = QLineEdit(self.settings.value('guid', backend.DEFAULT_GUID))
        self.guid.setPlaceholderText('相机已登记的 GUID')
        row.addWidget(self.ip)
        row.addWidget(self.guid)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.connect_button = QPushButton('连接并加载照片')
        self.connect_button.clicked.connect(self.load)
        self.all_button = QPushButton('全选')
        self.all_button.clicked.connect(lambda: self.select_all(True))
        self.none_button = QPushButton('清空选择')
        self.none_button.clicked.connect(lambda: self.select_all(False))
        self.folder_button = QPushButton('选择保存文件夹')
        self.folder_button.clicked.connect(self.choose_folder)
        self.download_button = QPushButton('下载勾选照片')
        self.download_button.clicked.connect(self.download)
        for button in (self.connect_button, self.all_button, self.none_button,
                       self.folder_button, self.download_button):
            row.addWidget(button)
        layout.addLayout(row)
        tools = QHBoxLayout()
        tools.addWidget(QLabel('拍摄日期'))
        self.days = QSpinBox()
        self.days.setRange(0, 36500)
        self.days.setSpecialValueText('全部日期')
        self.days.setSuffix(' 天内（含今天）')
        try:
            self.days.setValue(int(self.settings.value('days', 0)))
        except (ValueError, TypeError, OverflowError):
            self.days.setValue(0)
        tools.addWidget(self.days)
        tools.addWidget(QLabel('筛选后点“连接并加载照片”刷新'))
        self.new_button = QPushButton('选择尚未下载')
        self.new_button.clicked.connect(self.select_new)
        tools.addWidget(self.new_button)
        self.open_button = QPushButton('打开保存目录')
        self.open_button.clicked.connect(self.open_folder)
        tools.addWidget(self.open_button)
        self.cancel_button = QPushButton('取消本次任务')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        tools.addWidget(self.cancel_button)
        layout.addLayout(tools)
        self.output = Path(self.settings.value('output') or self.output)
        self.folder_label = QLabel(f'保存位置：{self.output}')
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setIconSize(QSize(170, 120))
        self.list.setGridSize(QSize(210, 175))
        self.list.setWordWrap(True)
        self.list.itemChanged.connect(self.update_count)
        self.list.itemDoubleClicked.connect(self.preview)
        layout.addWidget(self.list)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.count = QLabel('已勾选 0 张')
        layout.addWidget(self.count)
        self.status = QLabel('先连接相机，再勾选照片。自动发现时，点击连接后在相机上选「发送至电脑」。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def save_settings(self):
        for key, value in (('ip', self.ip.text().strip()), ('guid', self.guid.text().strip()),
                           ('days', self.days.value()), ('output', str(self.output))):
            self.settings.setValue(key, value)

    def open_folder(self):
        try:
            backend.prepare_output(self.output)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output))):
                raise backend.LocalFileError(f'无法打开保存目录：{self.output}')
        except backend.LocalFileError as error:
            self.status.setText(str(error))

    def select_new(self):
        conflicts = 0
        with QSignalBlocker(self.list):
            for index in range(self.list.count()):
                item = self.list.item(index)
                _, name, size = item.data(Qt.ItemDataRole.UserRole)
                try:
                    target = backend.raw_target(self.output, name)
                    saved = backend.raw_already_saved(target, size)
                except backend.LocalFileError:
                    conflicts += 1
                    saved = True
                item.setCheckState(Qt.CheckState.Unchecked if saved else Qt.CheckState.Checked)
        self.update_count()
        if conflicts:
            self.status.setText(f'有 {conflicts} 张 RAW 的文件名无效或目标路径冲突，请检查保存目录。')

    def preview(self, item):
        dialog = QDialog(self)
        dialog.setWindowTitle(item.text().split('\n')[0] + ' · 相机缩略图预览')
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = item.data(Qt.ItemDataRole.UserRole + 1)
        if pixmap is not None:
            label.setPixmap(pixmap.scaled(760, 520, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        else:
            label.setText('相机未提供可解码的缩略图')
        layout.addWidget(label)
        details = QLabel(item.text())
        if item.toolTip():
            details.setText(f'{item.text()}\n{item.toolTip()}')
        details.setWordWrap(True)
        layout.addWidget(details)
        dialog.exec()

    def cancel(self):
        self.cancel_event.set()
        camera = self.camera
        if camera is not None:
            camera.cancel_requested = True
        self.status.setText('正在取消，等待当前协议请求结束后保留连接。')
        self.cancel_button.setEnabled(False)

    def update_count(self):
        total = self.list.count()
        count = selected_size = 0
        for index in range(total):
            item = self.list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                count += 1
                selected_size += item.data(Qt.ItemDataRole.UserRole)[2]
        self.selected_count = count
        self.selected_size = selected_size
        self.show_count()

    def show_count(self):
        self.count.setText(f'已勾选 {self.selected_count} 张，共 {self.list.count()} 张；'
                           f'所选大小 {self.selected_size / 1048576:.1f} MiB')

    def select_all(self, selected):
        state = Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
        with QSignalBlocker(self.list):
            for index in range(self.list.count()):
                self.list.item(index).setCheckState(state)
        self.update_count()

    def choose_folder(self):
        directory = QFileDialog.getExistingDirectory(self, '选择 RAW 保存文件夹', str(self.output))
        if directory:
            self.output = Path(directory)
            self.folder_label.setText(f'保存位置：{self.output}')

    def start(self, task):
        self.busy = True
        self.save_settings()
        self.cancel_event.clear()
        if self.camera:
            self.camera.cancel_requested = False
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        for widget in (self.connect_button, self.download_button, self.folder_button,
                       self.all_button, self.none_button, self.ip, self.guid, self.days, self.new_button):
            widget.setEnabled(False)

        def execute():
            try:
                task()
            except backend.DownloadCancelled:
                self.events.put(('status', '本次任务已取消。' + (
                    '连接保留，可以继续操作。' if self.camera else '尚未连接，可再次点击连接。')))
            except Exception as error:
                if isinstance(error, backend.LocalFileError) or (
                        isinstance(error, backend.PTPResponseError) and error.result != 0x2003):
                    self.events.put(('status', f'操作失败：{error}'))
                else:
                    if self.camera:
                        self.camera.close()
                        self.camera = None
                    self.events.put(('error', str(error)))
            finally:
                if self.camera:
                    self.camera.cancel_requested = False
                self.events.put(('done', None))

        self.worker.submit(execute)

    def load(self):
        args = SimpleNamespace(ip=self.ip.text().strip() or None,
                               interface='0.0.0.0', guid=self.guid.text().strip(), timeout=20,
                               days=self.days.value())
        try:
            backend.parse_guid(args.guid)
        except ValueError as error:
            self.status.setText(str(error))
            return
        with QSignalBlocker(self.list):
            self.items_by_handle.clear()
            self.list.clear()
        self.update_count()
        self.status.setText('正在连接或刷新。需要重新连接时，请在相机上退出并重新选择「发送至电脑」。')
        self.start(lambda: self.load_worker(args))

    def load_worker(self, args):
        identity = (args.ip, args.guid)
        if self.camera is None or self.connection_identity != identity:
            if self.camera:
                self.camera.close()
                self.camera = None
            self.camera = backend.connect_camera(args, cancel_event=self.cancel_event)
            self.connection_identity = identity
            self.thumbnails_supported = True
        if self.cancel_event.is_set():
            raise backend.DownloadCancelled()
        total = 0
        missing = 0
        for handle, name, size, _ in backend.raw_objects(self.camera, args.days or None):
            thumb = b''
            if self.thumbnails_supported:
                try:
                    thumb = self.camera.operation(0x100a, [handle])
                except backend.PTPResponseError as error:
                    if error.result == 0x2005:  # OperationNotSupported
                        self.thumbnails_supported = False
                    elif error.result == 0x2009:  # InvalidObjectHandle
                        continue
                    elif error.result != 0x2010:  # NoThumbnailPresent
                        raise
            missing += not bool(thumb)
            total += 1
            self.events.put(('photo', (handle, name, size, thumb)))
        self.events.put(('status', f'加载完成，共 {total} 张 RAW；{missing} 张未取得缩略图。'))

    def download(self):
        chosen = [item.data(Qt.ItemDataRole.UserRole) for item in self.items_by_handle.values()
                  if item.checkState() == Qt.CheckState.Checked]
        if not chosen:
            self.status.setText('请先勾选需要下载的照片。')
            return
        if self.camera is None:
            self.status.setText('连接已失效，请重新连接并加载照片。')
            return
        output = self.output
        self.status.setText('正在下载，请保持相机连接。')
        self.start(lambda: self.download_worker(chosen, output))

    def download_worker(self, chosen, output):
        names = set()
        targets = []
        for _, name, _ in chosen:
            backend.check_duplicate_name(name, names)
            targets.append(backend.raw_target(output, name))
        backend.prepare_output(output)
        downloaded = skipped = 0
        total_bytes = sum(size for _, _, size in chosen)
        completed_bytes = 0
        downloaded_bytes = 0
        download_seconds = 0.0
        for (handle, name, size), target in zip(chosen, targets):
            if self.cancel_event.is_set():
                raise backend.DownloadCancelled()
            if backend.raw_already_saved(target, size):
                skipped += 1
            else:
                self.events.put(('status', f'正在下载 {name}（{downloaded + skipped + 1}/{len(chosen)}）'))
                started = time.monotonic()

                def report(received):
                    elapsed = max(time.monotonic() - started, 0.001)
                    self.events.put(('progress', (completed_bytes + received, total_bytes,
                                                  name, received / elapsed)))

                timing = backend.download_raw(self.camera, target, handle, size, progress=report)
                self.events.put(('saved', (handle, timing.describe(size))))
                downloaded += 1
                downloaded_bytes += size
                download_seconds += timing.elapsed
            completed_bytes += size
            self.events.put(('progress', (completed_bytes, total_bytes, name, 0)))
        timing = ''
        if downloaded:
            speed = downloaded_bytes / max(download_seconds, 0.001) / 1048576
            timing = f'下载耗时 {download_seconds:.1f} 秒，平均 {speed:.2f} MiB/s。'
        self.events.put(('status', f'完成：下载 {downloaded} 张，跳过已有文件 {skipped} 张。'
                                   f'{timing}保存于 {output}'))

    def poll(self):
        # 所有Qt控件只在主线程更新。
        photos_added = False
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'photo':
                handle, name, size, thumbnail = value
                item = QListWidgetItem(f'{name}\n{size / 1048576:.1f} MiB')
                item.setData(Qt.ItemDataRole.UserRole, (handle, name, size))
                self.items_by_handle[handle] = item
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                pixmap = QPixmap()
                if thumbnail and pixmap.loadFromData(thumbnail):
                    item.setIcon(QIcon(pixmap))
                    item.setData(Qt.ItemDataRole.UserRole + 1, pixmap)
                else:
                    item.setText(item.text() + '\n无可用缩略图')
                self.list.addItem(item)
                photos_added = True
            elif kind == 'saved':
                handle, details = value
                with QSignalBlocker(self.list):
                    self.items_by_handle[handle].setToolTip(details)
            elif kind == 'progress':
                completed, total, name, speed = value
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(completed * 1000 / total) if total else 1000)
                rate = f' · {speed / 1048576:.1f} MiB/s' if speed else ''
                self.status.setText(f'{name} · 总进度 {completed / 1048576:.1f}/{total / 1048576:.1f} MiB{rate}')
            elif kind == 'status':
                self.status.setText(value)
            elif kind == 'error':
                self.status.setText(f'操作失败：{value}。请重新连接相机。')
            elif kind == 'done':
                self.busy = False
                if self.close_pending:
                    self.close()
                    return
                self.cancel_button.setEnabled(False)
                self.progress.setRange(0, 1000)
                for widget in (self.connect_button, self.download_button, self.folder_button,
                               self.all_button, self.none_button, self.ip, self.guid, self.days, self.new_button):
                    widget.setEnabled(True)

        if photos_added:
            self.show_count()

    def closeEvent(self, event):
        if self.busy:
            self.close_pending = True
            self.cancel()
            event.ignore()
            return
        if self.camera:
            self.camera.close()
        self.save_settings()
        self.timer.stop()
        self.worker.shutdown(wait=False)
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    gallery = Gallery()
    gallery.show()
    sys.exit(app.exec())
