# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6-Essentials>=6.8,<7"]
# ///
"""运行 uv run sony_raw_gallery.py。将 sony_camera.py 放在同一目录。"""
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
import queue
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer, QSize, QSettings, QUrl
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QSpinBox, QProgressBar, QDialog,
)
import sony_camera as backend


class ProgressWriter:
    def __init__(self, stream, report):
        self.stream = stream
        self.report = report
        self.received = 0
        self.last_report = 0

    def write(self, data):
        written = self.stream.write(data)
        self.received += written
        if time.monotonic() - self.last_report > 0.2:
            self.report(self.received)
            self.last_report = time.monotonic()
        return written


class Gallery(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Sony RAW 照片浏览与下载')
        self.resize(1000, 760)
        self.settings = QSettings('SonyRawTools', 'Gallery')
        self.cancel_event = threading.Event()
        self.camera = None
        self.connection_identity = None
        self.records = {}
        self.events = queue.Queue()
        # 同一会话所有操作按顺序在一个后台线程运行。
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.busy = False
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
        self.days.setValue(int(self.settings.value('days', 0)))
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
        self.output = Path(self.settings.value('output', str(self.output)))
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
        self.output.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output)))

    def select_new(self):
        for index in range(self.list.count()):
            item = self.list.item(index)
            _, name, size = self.records[item.data(Qt.ItemDataRole.UserRole)]
            path = self.output / name
            exists = path.is_file() and path.stat().st_size == size
            item.setCheckState(Qt.CheckState.Unchecked if exists else Qt.CheckState.Checked)

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
        layout.addWidget(QLabel(item.text()))
        dialog.exec()

    def cancel(self):
        self.cancel_event.set()
        if self.camera:
            self.camera.cancel_requested = True
        self.status.setText('正在取消，等待当前协议请求结束后保留连接。')
        self.cancel_button.setEnabled(False)

    def update_count(self):
        count = sum(self.list.item(i).checkState() == Qt.CheckState.Checked
                    for i in range(self.list.count()))
        selected_size = sum(self.records[self.list.item(i).data(Qt.ItemDataRole.UserRole)][2]
                            for i in range(self.list.count())
                            if self.list.item(i).checkState() == Qt.CheckState.Checked)
        self.count.setText(f'已勾选 {count} 张，共 {self.list.count()} 张；所选大小 {selected_size / 1048576:.1f} MiB')

    def select_all(self, selected):
        state = Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

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
                self.events.put(('status', '本次任务已取消。' + ('连接保留，可以继续操作。' if self.camera else '尚未连接，可再次点击连接。')))
            except Exception as error:
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
        self.list.blockSignals(True)
        self.list.clear()
        self.records.clear()
        self.list.blockSignals(False)
        self.update_count()
        self.status.setText('正在连接或刷新。需要重新连接时，请在相机上退出并重新选择「发送至电脑」。')
        self.start(lambda: self.load_worker(args))

    def load_worker(self, args):
        earliest = date.today() - timedelta(days=args.days - 1) if args.days else None
        identity = (args.ip, args.guid)
        if self.camera is None or self.connection_identity != identity:
            if self.camera:
                self.camera.close()
                self.camera = None
            self.camera = backend.connect_camera(args, cancel_event=self.cancel_event)
            self.connection_identity = identity
        if self.cancel_event.is_set():
            raise backend.DownloadCancelled()
        total = 0
        missing = 0
        for storage in backend.numbers(self.camera.operation(0x1004)):
            handles = backend.numbers(self.camera.operation(0x1007, [storage, 0, 0]))
            for handle in handles:
                info = self.camera.operation(0x1008, [handle])
                if len(info) < 53:
                    raise ValueError('相机文件信息不完整')
                name, date_offset = backend.ptp_string(info, 52)
                if not name.lower().endswith('.arw'):
                    continue
                if earliest is not None:
                    captured, _ = backend.ptp_string(info, date_offset)
                    try:
                        captured_date = datetime.strptime(captured[:8], '%Y%m%d').date()
                    except ValueError:
                        continue
                    if not earliest <= captured_date <= date.today():
                        continue
                size = int.from_bytes(info[8:12], 'little')
                thumb = b''
                try:
                    # GetThumb：读取相机提供的缩略图，不下载整张RAW。
                    thumb = self.camera.operation(0x100a, [handle])
                except RuntimeError as error:
                    # 只对相机已完整返回的PTP错误保留会话，协议失步继续抛出。
                    if 'PTP 操作 100a 失败，返回码为' not in str(error):
                        raise
                missing += not bool(thumb)
                total += 1
                self.events.put(('photo', (handle, name, size, thumb)))
        self.events.put(('status', f'加载完成，共 {total} 张 RAW；{missing} 张未取得缩略图。'))

    def download(self):
        chosen = [self.records[self.list.item(i).data(Qt.ItemDataRole.UserRole)]
                  for i in range(self.list.count())
                  if self.list.item(i).checkState() == Qt.CheckState.Checked]
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
        output.mkdir(parents=True, exist_ok=True)
        downloaded = skipped = 0
        total_bytes = sum(size for _, _, size in chosen)
        completed_bytes = 0
        started = time.monotonic()
        for handle, name, size in chosen:
            if self.cancel_event.is_set():
                raise backend.DownloadCancelled()
            if (any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
                    or name.endswith((' ', '.'))
                    or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                        *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
                raise ValueError(f'无效的 Windows 文件名：{name}')
            target = output / name
            if target.exists():
                if target.is_file() and target.stat().st_size == size:
                    skipped += 1
                    completed_bytes += size
                    self.events.put(('progress', (completed_bytes, total_bytes, name, 0)))
                    continue
                raise FileExistsError(f'同名文件大小不同，请换保存目录或检查文件：{target}')
            self.events.put(('status', f'正在下载 {name}（{downloaded + skipped + 1}/{len(chosen)}）'))
            with tempfile.NamedTemporaryFile(mode='wb', dir=output, prefix=name + '.',
                                             suffix='.part', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    def report(received):
                        elapsed = max(time.monotonic() - started, 0.001)
                        self.events.put(('progress', (completed_bytes + received, total_bytes,
                                                     name, received / elapsed)))
                    self.camera.operation(0x1009, [handle], output=ProgressWriter(stream, report))
                except backend.DownloadCancelled:
                    stream.close()
                    temporary.unlink(missing_ok=True)
                    raise
                except Exception as error:
                    raise IOError(f'{error}；未完成文件保留于 {temporary}') from error
            if temporary.stat().st_size != size:
                raise IOError(f'文件大小不匹配，临时文件：{temporary}')
            os.rename(temporary, target)
            self.events.put(('progress', (completed_bytes + size, total_bytes, name, 0)))
            downloaded += 1
            completed_bytes += size
        self.events.put(('status', f'完成：下载 {downloaded} 张，跳过已有文件 {skipped} 张。保存于 {output}'))

    def poll(self):
        # 所有Qt控件只在主线程更新。
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'photo':
                handle, name, size, thumbnail = value
                self.records[handle] = (handle, name, size)
                item = QListWidgetItem(f'{name}\n{size / 1048576:.1f} MiB')
                item.setData(Qt.ItemDataRole.UserRole, handle)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                pixmap = QPixmap()
                if thumbnail and pixmap.loadFromData(thumbnail):
                    item.setIcon(QIcon(pixmap))
                    item.setData(Qt.ItemDataRole.UserRole + 1, pixmap)
                else:
                    item.setText(item.text() + '\n无可用缩略图')
                self.list.addItem(item)
                self.update_count()
            elif kind == 'progress':
                completed, total, name, speed = value
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(completed * 1000 / total) if total else 1000)
                self.status.setText(f'{name} · 总进度 {completed / 1048576:.1f}/{total / 1048576:.1f} MiB')
            elif kind == 'status':
                self.status.setText(value)
            elif kind == 'error':
                self.status.setText(f'操作失败：{value}。请重新连接相机。')
            elif kind == 'done':
                self.busy = False
                self.cancel_button.setEnabled(False)
                self.progress.setRange(0, 1000)
                for widget in (self.connect_button, self.download_button, self.folder_button,
                               self.all_button, self.none_button, self.ip, self.guid, self.days, self.new_button):
                    widget.setEnabled(True)

    def closeEvent(self, event):
        if self.busy:
            self.cancel()
            QMessageBox.information(self, '正在取消任务', '已请求取消。当前请求处理结束后即可关闭窗口。')
            event.ignore()
            return
        if self.camera:
            self.camera.close()
        self.save_settings()
        self.worker.shutdown(wait=False)
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    gallery = Gallery()
    gallery.show()
    sys.exit(app.exec())





