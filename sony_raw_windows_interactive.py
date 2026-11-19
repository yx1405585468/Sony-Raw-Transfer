import argparse
import sys
import signal
import os
from pathlib import Path
from datetime import date, datetime, timedelta
import socket
import struct
import time
import tempfile
import math
import urllib.request

DEFAULT_GUID = '00:00:00:00:00:00:00:00:ff:ff:c0:a5:e8:a4:34:8b'


class DownloadCancelled(Exception):
    pass


def exact(sock, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = sock.recv(min(size - len(parts), 1024 * 1024))
        if not chunk:
            raise ConnectionError('相机已关闭连接')
        parts.extend(chunk)
    return bytes(parts)


def send(sock, kind, body=b''):
    sock.sendall(struct.pack('<II', len(body) + 8, kind) + body)


def packet(sock):
    size, kind = struct.unpack('<II', exact(sock, 8))
    if size < 8 or size > 64 * 1024 * 1024:
        raise ValueError('PTP/IP 数据包长度无效')
    return kind, exact(sock, size - 8)


def discover(interface):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', 1900))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton('239.255.255.250') + socket.inet_aton(interface))
        print('正在等待相机，请在相机上选择「发送至电脑」。', flush=True)
        while True:
            data, address = sock.recvfrom(65535)
            message = data.decode('utf-8', errors='replace')
            if 'MtpNullService' in message and 'ssdp:alive' in message:
                return address[0]


class Camera:
    def __init__(self, ip, guid, timeout):
        identity = bytes.fromhex(guid.replace(':', '').replace('-', ''))
        if len(identity) != 16:
            raise ValueError('GUID 必须包含 16 个字节')
        self.command = socket.create_connection((ip, 15740), timeout)
        self.event = None
        self.transaction = 0
        self.cancel_requested = False
        try:
            send(self.command, 1, identity + (socket.gethostname() + '\0').encode('utf-16le')
                 + struct.pack('<I', 0x10000))
            kind, body = packet(self.command)
            if kind != 2 or len(body) < 4:
                raise ConnectionError(f'相机初始握手未通过，收到的包类型为 {kind}')
            connection = struct.unpack_from('<I', body)[0]
            self.event = socket.create_connection((ip, 15740), timeout)
            send(self.event, 3, struct.pack('<I', connection))
            if packet(self.event)[0] != 4:
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

    def operation(self, code, params=(), output=None):
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
        while True:
            kind, body = packet(self.command)
            if kind == 14:
                send(self.command, 15)
                continue
            minimum_size = {7: 6, 9: 12, 10: 4, 12: 4}.get(kind)
            if minimum_size is not None and len(body) < minimum_size:
                raise ValueError('PTP/IP 数据包内容不完整')
            transaction_offset = 2 if kind == 7 else 0
            if kind in (7, 9, 10, 12):
                if struct.unpack_from('<I', body, transaction_offset)[0] != transaction:
                    raise RuntimeError('PTP 事务编号不匹配')
            if kind == 9:
                if expected is not None or data_ended:
                    raise ValueError('重复的数据开始包')
                tid, expected = struct.unpack('<IQ', body)
            elif kind in (10, 12):
                tid = struct.unpack_from('<I', body)[0]
                if expected is None or data_ended:
                    raise ValueError('数据包顺序无效')
                chunk = body[4:]
                if received + len(chunk) > expected:
                    raise IOError('相机发送的数据超过声明长度')
                data_ended = kind == 12
                received += len(chunk)
                if output is None:
                    data.extend(chunk)
                else:
                    if not self.cancel_requested:
                        output.write(chunk)
                    if time.monotonic() - last_report > 1:
                        print(f'  下载进度：{received / 1048576:.1f} / {(expected or 0) / 1048576:.1f} MiB', flush=True)
                        last_report = time.monotonic()
            elif kind == 7:
                result, tid = struct.unpack_from('<HI', body)
                if result != 0x2001:
                    raise RuntimeError(f'PTP 操作 {code:04x} 失败，返回码为 {result:04x}')
                if expected is not None and (received != expected or not data_ended):
                    raise IOError(f'数据接收不完整，已收到 {received} 字节，应收到 {expected} 字节')
                if self.cancel_requested:
                    raise DownloadCancelled('已取消本次操作，连接保持。')
                return bytes(data)
            else:
                raise RuntimeError(f'收到未支持的数据包类型 {kind}')


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


def connect_camera(args):
    ip = args.ip or discover(args.interface)
    print(f'相机 IP 地址：{ip}', flush=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(10):
        try:
            with opener.open(f'http://{ip}:1900/DeviceDescription.xml', timeout=2) as response:
                response.read()
            break
        except OSError:
            if attempt == 9:
                raise
            time.sleep(1)
    return Camera(ip, args.guid, args.timeout)


def transfer(camera, args):
    # 信号处理器只设置标志，避免打断半个TCP包导致后续会话失步。
    def request_cancel(signum, frame):
        if not camera.cancel_requested:
            camera.cancel_requested = True
            print('\n正在取消：停止保存，正在接收并丢弃本次请求的剩余数据，请稍候。', flush=True)

    camera.cancel_requested = False
    previous_handler = signal.signal(signal.SIGINT, request_cancel)
    print('执行过程中按 Ctrl+C 可取消本次任务并保留连接。', flush=True)
    try:
        transfer_files(camera, args)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        camera.cancel_requested = False


def transfer_files(camera, args):
    today = date.today()
    if args.days is not None and not 1 <= args.days <= today.toordinal():
        raise ValueError('天数必须为正整数，且不能超出日期范围')
    earliest = today - timedelta(days=args.days - 1) if args.days else None
    if not args.list:
        args.output.mkdir(parents=True, exist_ok=True)
    for storage in numbers(camera.operation(0x1004)):
        for handle in numbers(camera.operation(0x1007, [storage, 0, 0])):
            info = camera.operation(0x1008, [handle])
            if len(info) < 53:
                raise ValueError('相机文件信息不完整')
            size = struct.unpack_from('<I', info, 8)[0]
            name, date_offset = ptp_string(info, 52)
            if not name.lower().endswith('.arw'):
                continue
            if earliest is not None:
                captured, _ = ptp_string(info, date_offset)
                try:
                    captured_date = datetime.strptime(captured[:8], '%Y%m%d').date()
                except ValueError:
                    print(f'跳过 {name}，拍摄日期缺失或无效（{captured!r}）')
                    continue
                if not earliest <= captured_date <= today:
                    continue
                print(f'拍摄日期：{captured_date}', flush=True)
            if any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name) or name.endswith((' ', '.')) or \
                    name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                                                   *(f'LPT{i}' for i in range(1, 10))}:
                raise ValueError('相机文件名包含不安全的路径字符')
            print(f'文件编号 {handle}：{name} ({size / 1048576:.1f} MiB)', flush=True)
            if args.list:
                continue
            target = args.output / name
            if target.exists():
                if target.is_file() and target.stat().st_size == size:
                    print('  文件已存在且大小一致，跳过下载')
                    continue
                raise FileExistsError(f'已存在同名文件，但大小不同：{target}')
            with tempfile.NamedTemporaryFile(mode='wb', dir=args.output,
                                             prefix=name + '.', suffix='.part', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    camera.operation(0x1009, [handle], output=stream)
                except DownloadCancelled:
                    stream.close()
                    temporary.unlink(missing_ok=True)
                    raise
                except BaseException:
                    print(f'未完成的文件保留于：{temporary}', flush=True)
                    raise
            if temporary.stat().st_size != size:
                raise IOError(f'下载文件大小不匹配：{temporary}')
            os.rename(temporary, target)
            print(f'已保存：{target}', flush=True)
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
            except DownloadCancelled as error:
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False, description='索尼相机 PTP/IP RAW 下载工具（仅使用 Python 标准库）')
    parser.add_argument('-h', '--help', action='help', help='显示帮助信息并退出')
    parser._positionals.title = '位置参数'
    parser._optionals.title = '可选参数'
    parser.add_argument('--ip', help='相机 IP 地址；省略时通过 SSDP 自动发现')
    parser.add_argument('--interface', default='0.0.0.0', help='接收组播所用的电脑 Wi-Fi IPv4 地址')
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
