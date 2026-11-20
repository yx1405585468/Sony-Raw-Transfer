import argparse
import sys
import signal
import os
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date, timedelta
import socket
import stat
import struct
import time
import tempfile
import math
import urllib.request

DEFAULT_GUID = '00:00:00:00:00:00:00:00:ff:ff:c0:a5:e8:a4:34:8b'
MAX_BUFFERED_PTP_BYTES = 64 * 1024 * 1024


class DownloadCancelled(Exception):
    pass


class PTPResponseError(RuntimeError):
    def __init__(self, operation, result, partial=None):
        self.operation = operation
        self.result = result
        message = f'PTP 操作 {operation:04x} 失败，返回码为 {result:04x}'
        if partial is not None:
            message += f'；下载临时文件：{partial}'
        super().__init__(message)


class LocalFileError(OSError):
    """文件操作失败时，PTP 仍在事务边界上。"""


@dataclass
class TransferTiming:
    started: float = field(default_factory=time.monotonic)
    first_byte: float | None = None
    last_byte: float | None = None
    elapsed: float = 0.0

    def describe(self, size):
        rate = size / (1024 * 1024) / max(self.elapsed, 0.001)
        summary = f'共 {self.elapsed:.2f} 秒，平均 {rate:.2f} MiB/s'
        if self.first_byte is not None and self.last_byte is not None:
            wait = self.first_byte - self.started
            receive = self.last_byte - self.first_byte
            finish = max(0.0, self.elapsed - wait - receive)
            summary += f'（首字节等待 {wait:.2f} 秒，接收 {receive:.2f} 秒，收尾 {finish:.2f} 秒）'
        return summary


def exact(read, size):
    data = read(min(size, 1024 * 1024))
    if len(data) == size:
        return data
    if not data:
        raise ConnectionError('相机已关闭连接')
    parts = bytearray(data)
    while len(parts) < size:
        chunk = read(min(size - len(parts), 1024 * 1024))
        if not chunk:
            raise ConnectionError('相机已关闭连接')
        parts.extend(chunk)
    return bytes(parts)


def send(sock, kind, body=b''):
    sock.sendall(struct.pack('<II', len(body) + 8, kind) + body)


def packet_header(read, streaming=False):
    size, kind = struct.unpack('<II', exact(read, 8))
    if size < 8 or (size > MAX_BUFFERED_PTP_BYTES and not (streaming and kind in (10, 12))):
        raise ValueError('PTP/IP 数据包长度无效')
    return kind, size - 8


def packet(read):
    kind, length = packet_header(read)
    return kind, exact(read, length)


def discover(interface, cancel_event=None):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', 1900))
        sock.settimeout(0.5)  # 定期检查取消请求。
        local_ips = [interface]
        if interface == '0.0.0.0':
            try:
                addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
            except socket.gaierror:
                addresses = []
            local_ips = sorted({
                address[0] for _, _, _, _, address in addresses
                if address[0] != '0.0.0.0' and not address[0].startswith('127.')
            })

            local_ips.append(interface)

        group = socket.inet_aton('239.255.255.250')
        joined = []
        for local_ip in local_ips:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                group + socket.inet_aton(local_ip))
            except OSError:
                if interface != '0.0.0.0' or (local_ip == interface and not joined):
                    raise
            else:
                joined.append(local_ip)
        print(f'SSDP IPv4: {", ".join(joined)}', flush=True)
        print('正在等待相机，请在相机上选择「发送至电脑」。', flush=True)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            message = data.decode('utf-8', errors='replace')
            if 'MtpNullService' in message and 'ssdp:alive' in message:
                return address[0]


def parse_guid(guid):
    try:
        identity = bytes.fromhex(guid.replace(':', '').replace('-', ''))
    except ValueError as error:
        raise ValueError('GUID 包含无效的十六进制字符') from error
    if len(identity) != 16:
        raise ValueError('GUID 必须包含 16 个字节')
    return identity


class Camera:
    def __init__(self, ip, guid, timeout):
        identity = parse_guid(guid)
        self.command = socket.create_connection((ip, 15740), timeout)
        self.event = None
        self.transaction = 0
        self.cancel_requested = False
        try:
            send(self.command, 1, identity + (socket.gethostname() + '\0').encode('utf-16le')
                 + struct.pack('<I', 0x10000))
            kind, body = packet(self.command.recv)
            if kind != 2 or len(body) < 4:
                raise ConnectionError(f'相机初始握手未通过，收到的包类型为 {kind}')
            connection = struct.unpack_from('<I', body)[0]
            self.event = socket.create_connection((ip, 15740), timeout)
            send(self.event, 3, struct.pack('<I', connection))
            if packet(self.event.recv)[0] != 4:
                raise ConnectionError('事件通道握手失败')
            self.operation(0x1002, [1])
            print('PTP 会话已建立', flush=True)
        except BaseException:
            self.close()
            raise

    def close(self):
        for sock in (self.event, self.command):
            if sock is not None:
                sock.close()

    def operation(self, code, params=(), output=None, *, file_size=None, timing=None, progress=None):
        if self.cancel_requested:
            raise DownloadCancelled('已取消本次操作，连接保持。')
        transaction = self.transaction
        self.transaction = (self.transaction + 1) & 0xffffffff
        send(self.command, 6, struct.pack('<IHI', 1, code, transaction)
             + b''.join(struct.pack('<I', value) for value in params))
        data = bytearray()
        received = 0
        expected = None
        last_report = 0
        data_ended = False
        buffer = memoryview(bytearray(256 * 1024)) if output is not None else None
        buffered = 0
        read = self.command.recv
        while True:
            kind, length = packet_header(read, streaming=buffer is not None)
            streamed = output is not None and kind in (10, 12)
            if kind == 14:
                if length:
                    raise ValueError('PTP/IP 探测包长度无效')
                send(self.command, 15)
                continue
            if kind not in (7, 9, 10, 12):
                raise RuntimeError(f'收到未支持的数据包类型 {kind}')
            if ((kind == 7 and length < 6) or (kind == 9 and length != 12)
                    or (kind in (10, 12) and length < 4)):
                raise ValueError('PTP/IP 数据包长度无效')
            if kind in (10, 12):
                if expected is None or data_ended:
                    raise ValueError('数据包顺序无效')
                if received + length - 4 > expected:
                    raise IOError('相机发送的数据超过声明长度')
            body = exact(read, 4 if streamed else length)
            transaction_offset = 2 if kind == 7 else 0
            if struct.unpack_from('<I', body, transaction_offset)[0] != transaction:
                raise RuntimeError('PTP 事务编号不匹配')
            if kind == 9:
                if expected is not None or data_ended:
                    raise ValueError('重复的数据开始包')
                expected = struct.unpack_from('<Q', body, 4)[0]
                if output is None and expected > MAX_BUFFERED_PTP_BYTES:
                    raise ValueError('PTP 元数据超过内存接收上限')
                if file_size is not None and expected != file_size:
                    raise IOError(f'相机声明的传输大小为 {expected} 字节，文件信息为 {file_size} 字节')
            elif kind in (10, 12):
                data_ended = kind == 12
                if streamed:
                    remaining = length - 4
                    while remaining:
                        end = min(buffered + remaining, len(buffer))
                        count = self.command.recv_into(buffer[buffered:end])
                        if not count:
                            raise ConnectionError('相机已关闭连接')
                        if timing is not None and received == 0:
                            timing.first_byte = time.monotonic()
                        received += count
                        if timing is not None and received == expected:
                            timing.last_byte = time.monotonic()
                        remaining -= count
                        if self.cancel_requested:
                            buffered = 0
                            continue
                        buffered += count
                        if buffered == len(buffer):
                            if output.write(buffer) != buffered:
                                raise OSError('文件写入不完整')
                            buffered = 0
                            if progress is not None:
                                now = time.monotonic()
                                if now - last_report >= 0.5:
                                    progress(received)
                                    last_report = now
                else:
                    data.extend(memoryview(body)[4:])
                    received += length - 4
            elif kind == 7:
                result = struct.unpack_from('<H', body)[0]
                if result != 0x2001:
                    raise PTPResponseError(code, result)
                if output is not None and expected is None:
                    raise IOError('相机未返回文件数据')
                if expected is not None and (received != expected or not data_ended):
                    raise IOError(f'数据接收不完整，已收到 {received} 字节，应收到 {expected} 字节')
                if self.cancel_requested:
                    raise DownloadCancelled('已取消本次操作，连接保持。')
                if buffered:
                    # 成功响应已读完，此处的本地写入错误允许继续使用会话。
                    try:
                        if output.write(buffer[:buffered]) != buffered:
                            raise OSError('文件写入不完整')
                    except OSError as error:
                        raise LocalFileError(str(error)) from error
                return bytes(data)


def numbers(data):
    if len(data) < 4:
        raise ValueError('缺少 PTP 数组长度')
    count = struct.unpack_from('<I', data)[0]
    if len(data) != 4 + count * 4:
        raise ValueError('PTP 数组格式无效')
    return struct.unpack_from(f'<{count}I', data, 4)


def ptp_string(data, offset):
    if offset < 0 or offset >= len(data):
        raise ValueError('缺少 PTP 字符串长度字段')
    count = data[offset]
    end = offset + 1 + count * 2
    if end > len(data):
        raise ValueError('PTP 字符串数据不完整')
    value = data[offset + 1:end].decode('utf-16le').rstrip('\0')
    return value, end


def connect_camera(args, cancel_event=None):
    parse_guid(args.guid)
    ip = args.ip or discover(args.interface, cancel_event)
    print(f'相机 IP 地址：{ip}', flush=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(10):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        try:
            with opener.open(f'http://{ip}:1900/DeviceDescription.xml', timeout=2) as response:
                response.read()
            break
        except OSError:
            if attempt == 9:
                raise
            if cancel_event is None:
                time.sleep(1)
            elif cancel_event.wait(1):
                raise DownloadCancelled()
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled()
    return Camera(ip, args.guid, args.timeout)


def transfer(camera, args):
    # 信号处理器只设置标志，避免打断半个TCP包导致后续会话失步。
    def request_cancel(signum, frame):
        camera.cancel_requested = True

    camera.cancel_requested = False
    previous_handler = signal.signal(signal.SIGINT, request_cancel)
    try:
        print('执行过程中按 Ctrl+C 可取消本次任务并保留连接。', flush=True)
        transfer_files(camera, args)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        camera.cancel_requested = False


def raw_objects(camera, days=None):
    today = date.today()
    if days is not None and not 1 <= days <= today.toordinal():
        raise ValueError('天数必须为正整数，且不能超出日期范围')
    earliest = today - timedelta(days=days - 1) if days is not None else None
    seen = set()
    for storage in numbers(camera.operation(0x1004)):
        for handle in numbers(camera.operation(0x1007, [storage, 0, 0])):
            if handle in seen:
                continue
            seen.add(handle)
            try:
                info = camera.operation(0x1008, [handle])
            except PTPResponseError as error:
                if error.result == 0x2009:  # 列出句柄后，照片已被移除。
                    continue
                raise
            if len(info) < 53:
                raise ValueError('相机文件信息不完整')
            name, date_offset = ptp_string(info, 52)
            if not name.lower().endswith('.arw'):
                continue
            captured_date = None
            if earliest is not None:
                try:
                    captured, _ = ptp_string(info, date_offset)
                    compact_date = captured[:8]
                    if len(compact_date) != 8 or not compact_date.isascii() or not compact_date.isdigit():
                        raise ValueError('拍摄日期格式无效')
                    captured_date = date.fromisoformat(compact_date)
                except ValueError:
                    print(f'跳过 {name}，拍摄日期缺失或无效', flush=True)
                    continue
                if not earliest <= captured_date <= today:
                    continue
            size = struct.unpack_from('<I', info, 8)[0]
            yield handle, name, size, captured_date


def raw_target(directory, name):
    stem = name.split('.')[0].rstrip(' ').upper()
    if (not name or name.endswith((' ', '.'))
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
            or stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
            or (len(stem) == 4 and stem[:3] in {'COM', 'LPT'} and stem[3] in '123456789¹²³')):
        raise LocalFileError(f'无效的 Windows 文件名：{name!r}')
    return directory / name


def prepare_output(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LocalFileError(f'无法打开保存目录 {directory}：{error}') from error


def check_duplicate_name(name, seen):
    key = name.casefold()
    if key in seen:
        raise LocalFileError(f'相机中有多张同名 RAW：{name}。请分开下载到不同目录。')
    seen.add(key)


def raw_already_saved(target, size):
    try:
        existing = target.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LocalFileError(f'无法检查目标文件 {target}：{error}') from error
    if stat.S_ISREG(existing.st_mode) and existing.st_size == size:
        return True
    raise LocalFileError(f'目标路径已被同名文件或目录占用：{target}')


def download_raw(camera, target, handle, size, wrap_output=None, *, progress=None):
    timing = TransferTiming()
    try:
        stream = tempfile.NamedTemporaryFile(mode='wb', dir=target.parent,
                                             prefix='.sony-', suffix='.part', delete=False)
    except OSError as error:
        raise LocalFileError(f'无法创建下载临时文件：{error}') from error
    temporary = Path(stream.name)
    transfer_complete = False
    try:
        with stream:
            output = wrap_output(stream) if wrap_output else stream
            camera.operation(0x1009, [handle], output=output, file_size=size,
                             timing=timing, progress=progress)
            transfer_complete = True
    except DownloadCancelled as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise LocalFileError(f'下载已取消，但无法删除临时文件 {temporary}：{cleanup_error}') from error
        raise
    except PTPResponseError as error:
        raise PTPResponseError(error.operation, error.result, partial=temporary) from error
    except LocalFileError as error:
        raise LocalFileError(f'{error}；临时文件保留于 {temporary}') from error
    except Exception as error:
        message = f'{error}；未完成的文件保留于 {temporary}'
        if transfer_complete:
            raise LocalFileError(message) from error
        raise IOError(message) from error

    try:
        if temporary.stat().st_size != size:
            raise OSError('下载文件大小与相机文件信息不一致')
        if os.name == 'nt':
            # Windows 的 rename 在目标已存在时失败。
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
            temporary.unlink()
    except OSError as error:
        raise LocalFileError(f'{error}；下载临时文件：{temporary}') from error
    timing.elapsed = time.monotonic() - timing.started
    return timing


def transfer_files(camera, args):
    if not args.list:
        prepare_output(args.output)
    names = set()
    for handle, name, size, captured_date in raw_objects(camera, args.days):
        target = raw_target(args.output, name)
        if captured_date:
            print(f'拍摄日期：{captured_date}', flush=True)
        print(f'文件编号 {handle}：{name} ({size / 1048576:.1f} MiB)', flush=True)
        if args.list:
            continue
        check_duplicate_name(name, names)
        if raw_already_saved(target, size):
            print('  文件已存在且大小一致，跳过下载')
            continue

        def report(received):
            print(f'  下载进度：{received / 1048576:.1f} / {size / 1048576:.1f} MiB', flush=True)

        timing = download_raw(camera, target, handle, size, progress=report)
        print(f'已保存：{target}，{timing.describe(size)}', flush=True)
    print('本次操作完成。', flush=True)


def run(args):
    camera = connect_camera(args)
    try:
        transfer(camera, args)
    finally:
        camera.close()


def interactive(args):
    camera = None
    menu = '\n索尼相机 RAW 工具\n1  连接相机（自动发现；相机准备好后选择「发送至电脑」）\n2  列出全部 RAW\n3  列出最近 N 天的 RAW\n4  下载全部 RAW（跳过同名且大小一致的文件）\n5  下载最近 N 天的 RAW（包含今天）\n6  断开连接\n0  退出\n'
    try:
        while True:
            print(menu)
            print('连接状态：' + ('已建立，执行时将验证是否仍可用' if camera else '未连接'))
            try:
                choice = input('请输入功能编号：').strip()
                if choice == '0':
                    break
                if choice not in ('1', '2', '3', '4', '5', '6'):
                    print('请输入菜单中的编号。')
                    continue
                if choice == '6':
                    if camera:
                        camera.close()
                        camera = None
                    print('已断开连接。')
                    continue
                args.days = None
                if choice in ('3', '5'):
                    value = input('请输入天数（正整数，包含今天）：').strip()
                    if not value.isascii() or not value.isdecimal() or len(value) > 7 or not 1 <= int(
                            value) <= date.today().toordinal():
                        print('天数无效，请输入有效范围内的正整数。')
                        continue
                    args.days = int(value)
                args.list = choice in ('2', '3')
                if camera is None:
                    camera = connect_camera(args)
                if choice == '1':
                    camera.operation(0x1004)
                    print('相机连接正常，请选择下一项功能。')
                else:
                    transfer(camera, args)
            except PTPResponseError as error:
                print(error)
                if error.result == 0x2003 and camera is not None:  # SessionNotOpen
                    camera.close()
                    camera = None
            except (DownloadCancelled, LocalFileError) as error:
                print(error)
            except EOFError:
                break
            except (Exception, KeyboardInterrupt) as error:
                print(f'本次操作已停止：{error or "用户取消"}')
                if camera:
                    camera.close()
                    camera = None
                print('已返回菜单。再次连接前，请在相机上退出并重新选择「发送至电脑」。')
    finally:
        if camera:
            camera.close()
    print('程序已退出。')


def main():
    parser = argparse.ArgumentParser(add_help=False, description='索尼相机 PTP/IP RAW 下载工具（仅使用 Python 标准库）')
    parser.add_argument('-h', '--help', action='help', help='显示帮助信息并退出')
    parser._positionals.title = '位置参数'
    parser._optionals.title = '可选参数'
    parser.add_argument('--ip', help='相机 IP 地址；省略时通过 SSDP 自动发现')
    parser.add_argument('--interface', default='0.0.0.0', help='接收组播所用的电脑 IPv4 地址，默认自动识别')
    parser.add_argument('--guid', default=DEFAULT_GUID, help='相机已登记的接收端 GUID')
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'SonyPhotos',
                        help='照片保存目录，默认为脚本旁的 SonyPhotos')
    parser.add_argument('--timeout', type=float, default=20, help='网络操作超时秒数，默认 20 秒')
    parser.add_argument('--list', action='store_true', help='仅列出 RAW 文件')
    parser.add_argument('--days', type=int, help='筛选最近 N 个自然日的 RAW，包含今天')
    arguments = parser.parse_args()
    if arguments.days is not None and not 1 <= arguments.days <= date.today().toordinal():
        parser.error('--days 必须是有效日期范围内的正整数')
    if not math.isfinite(arguments.timeout) or arguments.timeout <= 0:
        parser.error('--timeout 必须是有限的正数')
    try:
        if len(sys.argv) == 1:
            interactive(arguments)
        else:
            run(arguments)
    except DownloadCancelled as error:
        print(error)
    except KeyboardInterrupt:
        print('\n已停止；未完成的下载保留为 .part 临时文件。')
    except Exception as error:
        parser.exit(1, f'错误：{error}\n')


if __name__ == '__main__':
    main()
