#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
초저부하 서버 관리 TUI
- 웹서버/브라우저 없음: 터미널(curses)만 사용
- 10초 주기 갱신 (CPU/GPU/사용자별 python/ipynb 카운트, kill 선택)
- 의존성: psutil, (선택) pynvml
- 권한: 타 사용자 프로세스 kill은 sudo로 실행 필요
키:
  ↑/k: 위로, ↓/j: 아래로, SPACE: 선택/해제
  t: 선택한 PID에 SIGTERM, x: SIGKILL, i: SIGINT
  c: 선택 해제, r: 즉시 새로고침, q: 종료
"""
import os, time, signal, curses, locale, shutil
from typing import List, Dict, Any, Tuple
import psutil
import pwd
from collections import defaultdict
"""
def prompt_input(stdscr, y, prompt, w):
    curses.echo()
    # 프롬프트 줄 지우고 입력 받기
    draw_text(stdscr, y, 0, " " * (w-1))
    draw_text(stdscr, y, 0, prompt, w-1)
    stdscr.refresh()
    try:
        s = stdscr.getstr(y, len(prompt), 64).decode("utf-8", "ignore").strip()
    except Exception:
        s = ""
    curses.noecho()
    return s
"""
def prompt_input(stdscr, y, prompt, w):
    # 블로킹 입력 모드로 전환
    stdscr.nodelay(False)
    curses.echo()
    try:
        curses.curs_set(1)
        stdscr.move(y, 0); stdscr.clrtoeol()
        draw_text(stdscr, y, 0, prompt, w-1)
        stdscr.refresh()
        s = stdscr.getstr(y, len(prompt), 64).decode("utf-8", "ignore").strip()
    except Exception:
        s = ""
    finally:
        curses.noecho()
        curses.curs_set(0)
        stdscr.nodelay(True)  # 원복
    return s


# ---- GPU (NVML) ----
_NVML_OK = False
try:
    import pynvml
    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _NVML_OK = False

REFRESH_SEC = 5
MAX_ROWS = 300  # 프로세스 표시는 상위 N개만 (부하 절감)
PROC_SORT_KEY = "cpu"  # "cpu" or "rss"

locale.setlocale(locale.LC_ALL, '')

def human_mb(x): return f"{int(x)}MB"
def esc(s): return (s or "").replace("\n", " ")[:200]

# ----- heuristics -----
def is_python_proc(p: psutil.Process) -> bool:
    try:
        name = (p.name() or "").lower()
        if "python" in name: return True
        cmd = " ".join(p.cmdline()).lower()
        return "python" in cmd
    except Exception:
        return False

def is_ipynb_kernel(p: psutil.Process) -> bool:
    try:
        cmd = " ".join(p.cmdline()).lower()
        return ("ipykernel" in cmd) or ("-m ipykernel" in cmd) or ("jupyter-kernel" in cmd)
    except Exception:
        return False

# ----- metrics -----
def get_cpu_info():
    physical = psutil.cpu_count(logical=False) or 0
    logical = psutil.cpu_count(logical=True) or 0
    total = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    return physical, logical, total, per_core

def get_gpu_info():
    if not _NVML_OK: return []
    out = []
    try:
        n = pynvml.nvmlDeviceGetCount()
        for i in range(n):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)#.decode("utf-8", errors="ignore")
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            out.append({
                "idx": i,
                "name": name,
                "mem_used_mb": mem.used // (1024**2),
                "mem_total_mb": mem.total // (1024**2),
                "util": getattr(util, "gpu", 0),
                "mem_util": getattr(util, "memory", 0),
                "temp": temp,
            })
    except Exception:
        return []
    return out

# UID 빠르게 얻기 (psutil.uids 우선, 없으면 pwd 조회)
def _get_uid_from_pinfo(pinfo):
    uids = pinfo.get("uids")
    if uids and getattr(uids, "real", None) is not None:
        return uids.real
    user = pinfo.get("username")
    if user:
        try:
            return pwd.getpwnam(user).pw_uid
        except KeyError:
            return None
    return None

def _is_human_uid(uid: int) -> bool:
    return uid is not None and uid >= 1000

def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

MAX_DISKS = 5  # 표시할 디스크 최대 개수

def human_gb(x: int) -> str:
    return f"{x / (1024**3):.1f}G"

def get_mem_disk_info():
    """
    - RAM/Swap: psutil 한 번씩 호출
    - 디스크: 즐겨찾는 마운트 몇 개만 우선 확인(부하↓), 부족하면 파티션에서 최대 MAX_DISKS까지 채움
    - tmpfs/devtmpfs/overlay 등은 제외
    """
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()

    mem = {
        "total": vm.total,
        "used": vm.used,
        "percent": vm.percent,
        "avail": vm.available,
    }
    swap = {
        "total": sm.total,
        "used": sm.used,
        "percent": sm.percent,
    }

    # 우선 확인할 후보 마운트
    preferred = [p for p in ["/", "/home", "/data", "/mnt", "/mnt/nas"] if os.path.ismount(p)]

    disks = []
    seen = set()

    for mp in preferred:
        try:
            du = psutil.disk_usage(mp)
            disks.append({"mount": mp, "total": du.total, "used": du.used, "percent": du.percent})
            seen.add(mp)
            if len(disks) >= MAX_DISKS:
                return mem, swap, disks
        except Exception:
            pass

    # 부족하면 실제 파티션에서 채우기
    try:
        for part in psutil.disk_partitions(all=False):
            mp = part.mountpoint
            if mp in seen:
                continue
            if part.fstype in ("tmpfs", "devtmpfs", "squashfs", "overlay", "aufs"):
                continue
            try:
                du = psutil.disk_usage(mp)
                if du.total == 0:
                    continue
                disks.append({"mount": mp, "total": du.total, "used": du.used, "percent": du.percent})
                seen.add(mp)
                if len(disks) >= MAX_DISKS:
                    break
            except Exception:
                continue
    except Exception:
        pass

    return mem, swap, disks

def get_gpu_user_usage():
    """
    GPU별로 실행 중인 프로세스 PID를 NVML에서 받아
    사용자별 VRAM 사용량(MB)과 PID 리스트를 집계한다.
    UID<1000(시스템 유저)는 제외.
    반환: {gpu_idx: {username: {"mem_mb": int, "pids": [int, ...]}, ...}, ...}
    """
    if not _NVML_OK:
        return {}

    usage = {}
    try:
        n = pynvml.nvmlDeviceGetCount()
        for i in range(n):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            agg = defaultdict(lambda: {"mem_mb": 0, "pids": []})

            # compute 프로세스
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses_v3(h)
            except Exception:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses(h)

            # (선택) 그래픽 프로세스도 포함하고 싶으면 주석 해제
            try:
                gprocs = pynvml.nvmlDeviceGetGraphicsRunningProcesses(h)
            except Exception:
                gprocs = []

            for pp in list(procs) + list(gprocs):
                pid = getattr(pp, "pid", None)
                if pid is None:
                    continue
                try:
                    p = psutil.Process(pid)
                    u = p.username()
                    # UID<1000 숨김
                    uid = p.uids().real if hasattr(p, "uids") else None
                    if uid is None:
                        import pwd
                        try:
                            uid = pwd.getpwnam(u).pw_uid
                        except Exception:
                            uid = None
                    if uid is not None and uid < 1000:
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                # VRAM(MB). NOT_AVAILABLE이면 0 처리
                mem = getattr(pp, "usedGpuMemory", 0)
                if isinstance(mem, int) and mem > 0:
                    mem_mb = mem // (1024**2)
                else:
                    mem_mb = 0
                agg[u]["mem_mb"] += mem_mb
                agg[u]["pids"].append(pid)

            if agg:
                usage[i] = dict(agg)
    except Exception:
        return {}
    return usage



def get_user_counts_and_processes() -> Tuple[Dict[str, Dict[str,int]], List[Dict[str,Any]]]:
    """
    사용자별 python/ipynb 카운트 + kill 후보 리스트
    - UID < 1000 (시스템 유저) 는 제외
    - 부하 절감을 위해 필요한 attr만 조회
    """
    counts: Dict[str, Dict[str,int]] = {}
    procs: List[Dict[str,Any]] = []

    for p in psutil.process_iter(attrs=["pid","username","uids","name","cmdline","memory_info"]):
        try:
            uid = _get_uid_from_pinfo(p.info)
            if not _is_human_uid(uid):
                continue  # 시스템 유저 제외

            user = p.info.get("username") or f"uid:{uid}"
            if user not in counts:
                counts[user] = {"python": 0, "ipynb": 0}

            typ = None
            if is_ipynb_kernel(p):
                counts[user]["ipynb"] += 1
                typ = "ipynb"
            elif is_python_proc(p):
                counts[user]["python"] += 1
                typ = "python"

            if typ:
                cpu = p.cpu_percent(None)  # non-blocking
                rss = (p.info.get("memory_info").rss if p.info.get("memory_info") else 0) // (1024**2)
                name = p.info.get("name") or ""
                cmd = " ".join(p.info.get("cmdline") or [])
                procs.append({
                    "pid": p.info["pid"], "user": user, "type": typ,
                    "cpu": cpu, "rss": rss, "name": name, "cmd": cmd
                })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 상위 N개만 정렬 유지
    if PROC_SORT_KEY == "cpu":
        procs.sort(key=lambda x: (x["cpu"], x["rss"]), reverse=True)
    else:
        procs.sort(key=lambda x: (x["rss"], x["cpu"]), reverse=True)
    if len(procs) > MAX_ROWS:
        procs = procs[:MAX_ROWS]
    return counts, procs


# ----- drawing -----
def draw_text(win, y, x, text, width=None, attr=0):
    if width is not None:
        text = (text[:width-1] + "…") if len(text) > width else text
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass

def run(stdscr):
    user_filter = None            # None이면 전체, 아니면 해당 사용자만
    type_filter = "all"           # "all" | "ipynb" | "python"
    global PROC_SORT_KEY
    curses.curs_set(0)
    stdscr.nodelay(True)
    
    # ==== 색상 초기화 ====
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED,    -1)
        curses.init_pair(2, curses.COLOR_GREEN,  -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        # ORANGE: 256컬러(208) 지원 시 사용, 아니면 YELLOW로 폴백
        if getattr(curses, "COLORS", 8) >= 256:
            try:
                curses.init_pair(4, 208, -1)  # 주황(Orange)
                ORANGE = curses.color_pair(4) | curses.A_BOLD
            except Exception:
                ORANGE = curses.color_pair(3) | curses.A_BOLD
        else:
            ORANGE = curses.color_pair(3) | curses.A_BOLD

        curses.init_pair(5, curses.COLOR_WHITE, -1)  # CPU 라벨용 흰색 볼드
        RED    = curses.color_pair(1) | curses.A_BOLD
        GREEN  = curses.color_pair(2)
        YELLOW = curses.color_pair(3) | curses.A_BOLD
        WHITEB = curses.color_pair(5) | curses.A_BOLD
        WHITE  = curses.color_pair(6)              # ← 볼드 없음
    else:
        RED = curses.A_BOLD
        GREEN = 0
        YELLOW = curses.A_BOLD
        ORANGE = curses.A_BOLD
        WHITEB = curses.A_BOLD
        WHITE = 0

    THRESH_G = 50   # ≤50% → 초록
    THRESH_Y = 70   # ≥70% → 노랑
    THRESH_O = 80   # ≥80% → 주황
    THRESH_R = 90   # ≥90% → 빨강

    def pct_attr(pct):
        try:
            p = float(pct)
        except Exception:
            return 0
        # 0~10%는 흰색(볼드 아님)으로 고정
        if p >= THRESH_R: return RED
        if p >= THRESH_O: return ORANGE
        if p >= THRESH_Y: return YELLOW
        else: #if p <= THRESH_G:
            if p == 0: return WHITE 
            return GREEN
        return 0

    # 고정폭 셀 출력 헬퍼
    def draw_cell(y, x, text, colw, attr=0):
        s = str(text)
        if len(s) > colw:
            s = s[:colw]              # 넘치면 잘라냄
        else:
            s = s.ljust(colw)         # 부족하면 공백 채움
        draw_text(stdscr, y, x, s, attr=attr)

    # 이어 그리기 헬퍼
    def add(y, x, text, attr=0):
        draw_text(stdscr, y, x, text, attr=attr)
        return x + len(text)
    

    # 최초 1회 prime: per-core 샘플링(아주 짧게)
    psutil.cpu_percent(interval=0.15, percpu=True)
    # 프로세스 cpu_percent prime: python/ipynb만 간단히 스캔
    for p in psutil.process_iter(attrs=["pid","name","cmdline"]):
        try:
            if is_python_proc(p) or is_ipynb_kernel(p):
                p.cpu_percent(None)
        except Exception:
            continue

    selected = set()   # 선택된 PID
    cursor = 0
    last_refresh = 0
    cpu_info = (0,0,0.0,[])
    gpu_info = []
    users = {}
    plist: List[Dict[str,Any]] = []

    while True:
        now = time.time()
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # 갱신
        if now - last_refresh >= REFRESH_SEC:
            cpu_info = get_cpu_info()
            gpu_info = get_gpu_info()
            mem_info, swap_info, disks_info = get_mem_disk_info()  # <-- 추가
            gpu_users = get_gpu_user_usage()
            users, plist = get_user_counts_and_processes()
            last_refresh = now

        # 헤더
        #draw_text(stdscr, 0, 0, "Mini Server TUI  ─  q:quit  SPACE:select  t:TERM  x:KILL  i:INT  r:refresh  c:clear", w-1, curses.A_BOLD)
        draw_text(
            stdscr, 0, 0,
            "Mini Server TUI ─ q:quit ↑/k,↓/j:move SPACE:select t/x/i:TERM/KILL/INT r:refresh c:clear "
            "| s:sort F:filter A:all-users v:type(0:all/1:ipynb/2:python)",
            w-1, curses.A_BOLD
        )

    
        # CPU 박스
        phy, logi, cpu_total, per_core = cpu_info
        draw_text(stdscr, 2, 0, f"[CPU] physical:{phy}  logical:{logi}  total:")
        draw_text(stdscr, 2, 39, f"{cpu_total:.1f}%", attr=pct_attr(cpu_total))

        

        # 표 형태: 12칸씩, 고정폭으로 정렬
        row = 3
        COLS_PER_ROW = 11
        COLW  = 11     # 셀 전체 폭
        LBLW  = 6      # 라벨 폭: "#xxx:" = 5
        VALW  = COLW - LBLW  # 값 폭

        for group in _chunk(list(enumerate(per_core)), COLS_PER_ROW):
            x = 2
            for i, v in group:
                label = f" #{i:>3}:"
                val   = f"{v:>3.0f}%"
                draw_cell(row, x,     label, LBLW, attr=WHITEB)   # 흰색 볼드 라벨
                draw_cell(row, x+LBLW, val,   VALW, attr=pct_attr(v))  # 값에 색
                x += COLW
            row += 1

        # GPU 박스 시작 위치(코어 라인 수에 따라 자동 조정)
        gy = row + 1
        
        if _NVML_OK and gpu_info:
            draw_text(stdscr, gy, 0, "[GPU] NVML OK")
            for i, g in enumerate(gpu_info):
                y = gy + 1 + i
                x = 2

                # 라벨은 기본, 'util %'만 색상 적용
                x = add(y, x, f" GPU{g['idx']} {g['name']} | util ")
                x = add(y, x, f"{g['util']}%", attr=pct_attr(g['util']))

                # 메모리 사용률(%) 계산해서 그 부분만 색상 적용
                mem_pct = int(round(100 * g['mem_used_mb'] / g['mem_total_mb'])) if g['mem_total_mb'] else 0
                x = add(y, x, " | mem ")
                x = add(y, x, f"{g['mem_used_mb']}/{g['mem_total_mb']} MB ", attr=pct_attr(mem_pct))
                x = add(y, x, f"({mem_pct}%)", attr=pct_attr(mem_pct))

                # 온도는 색 적용 안 함
                if g['temp'] is not None:
                    x = add(y, x, " | temp ")
                    x = add(y, x, f"{g['temp']}C")

                # (선택) 사용자별 VRAM 집계 표시 — 색 없음
                gu = gpu_users.get(g['idx'], {}) if 'gpu_users' in locals() else {}
                if gu:
                    top = sorted(gu.items(), key=lambda kv: -kv[1]["mem_mb"])[:3]
                    who = "  ".join(f"{name}:{st['mem_mb']}MB({len(st['pids'])}p)" for name, st in top)
                    x = add(y, x, "  | users " + who)

            gy = gy + 1 + len(gpu_info)
        else:
            draw_text(stdscr, gy, 0, "[GPU] NVML 비활성 또는 GPU 없음")
            gy += 1
            
        # MEMORY / SWAP
        draw_text(stdscr, gy+1, 0, "[MEMORY]")
        draw_text(stdscr, gy+2, 2,
                f"RAM  {human_gb(mem_info['used'])}/{human_gb(mem_info['total'])} ({mem_info['percent']:.0f}%)",
                attr=pct_attr(mem_info['percent']))
        draw_text(stdscr, gy+3, 2,
                f"SWAP {human_gb(swap_info['used'])}/{human_gb(swap_info['total'])} ({swap_info['percent']:.0f}%)",
                attr=pct_attr(swap_info['percent']))        
        
        # DISKS
        gy = gy + 5
        draw_text(stdscr, gy, 0, "[DISKS]")
        dy = gy + 1
        for d in disks_info:
            line = f"{d['mount']:<12}  {human_gb(d['used']):>8}/{human_gb(d['total']):>8}  ({d['percent']:.0f}%)"
            draw_text(stdscr, dy, 2, line, w-4, attr=pct_attr(d['percent']))
            dy += 1

        gy = dy  # 이후 섹션 시작 위치 갱신

        # 사용자 카운트
        draw_text(stdscr, gy+1, 0, "[USERS] python / ipynb (상위)")
        uline = gy+2
        for idx,(u,cnt) in enumerate(sorted(users.items(), key=lambda kv: (kv[1]['python']+kv[1]['ipynb']), reverse=True)):
            if uline+idx >= h-8: break
            draw_text(stdscr, uline+idx, 2, f"{u:>12}: py {cnt['python']:>3} | ipynb {cnt['ipynb']:>3}")

        # 프로세스 테이블 헤더
        table_y = uline + min(len(users), max(0, h - (uline+10))) + 3
        if table_y < gy+4:
            table_y = gy+4
        draw_text(
            stdscr, table_y, 0,
            "[PROCESSES] (python/ipynb only)  "
            f"sort:{PROC_SORT_KEY}  "
            f"user:{user_filter if user_filter else 'ALL'}  "
            f"type:{type_filter}  "
            f"updated:{time.strftime('%H:%M:%S', time.localtime(last_refresh+9*3600))}",
            w-1
        )        
        hdr = " Sel PID      User          Type   CPU%   RSS(MB)  Name / Cmd"
        draw_text(stdscr, table_y+1, 0, hdr, w-1, curses.A_UNDERLINE)

        # 표 표시 영역 계산 직전에 추가
        view = [p for p in plist
                if (user_filter is None or p["user"] == user_filter)
                and (type_filter == "all" or p["type"] == type_filter)]


        # 표 표시 영역
        rows_avail = h - (table_y+3)
        start = max(0, min(cursor - rows_avail//2, max(0, len(view)-rows_avail)))
        end = min(len(view), start + rows_avail)

        for i, p in enumerate(view[start:end], start=start):
            y = table_y + 2 + (i - start)
            sel = "■" if p["pid"] in selected else "□"

            # 좌우 반전(커서 선택) 여부
            row_attr = curses.A_REVERSE if i == cursor else 0

            # 1) 왼쪽: 선택박스~TYPE까지
            left = f" {sel}  {p['pid']:<8}  {p['user'][:12]:<12}  {p['type']:<6} "
            draw_text(stdscr, y, 0, left, w-1, attr=row_attr)

            # 2) CPU% 부분 — 색상 덧씌움
            cpu_str = f"{p['cpu']:>5.1f}"
            draw_text(stdscr, y, len(left), cpu_str, attr=row_attr | pct_attr(p['cpu']))

            # 3) CPU 뒤의 나머지(RSS, Name)
            rest = f"  {p['rss']:>7}  {esc(p['name'])}"
            draw_text(stdscr, y, len(left) + len(cpu_str), rest, w-1, attr=row_attr)

            # 4) 한 줄에 다 안 나오면 CMD 일부 추가
            right = f"  — {esc(p['cmd'])}"
            if len(left) + len(cpu_str) + len(rest) < w - 2:
                draw_text(stdscr, y, len(left) + len(cpu_str) + len(rest), right, w-1, attr=row_attr)

        stdscr.refresh()

        # 입력 처리 (논블로킹)
        ch = stdscr.getch()
        if ch == -1:
            time.sleep(0.05)
            continue

        if ch in (ord('q'), 27):  # ESC
            break
        elif ch in (curses.KEY_DOWN, ord('j')):
            if cursor < max(0, len(plist)-1): cursor += 1
        elif ch in (curses.KEY_UP, ord('k')):
            if cursor > 0: cursor -= 1
        elif ch == ord(' '):
            if 0 <= cursor < len(plist):
                pid = view[cursor]["pid"]
                if pid in selected: selected.remove(pid)
                else: selected.add(pid)
        elif ch == ord('c'):
            selected.clear()
        elif ch == ord('r'):
            last_refresh = 0  # 즉시 갱신
        elif ch == ord('s'):
            # 정렬 토글 (cpu <-> rss)
            #global PROC_SORT_KEY
            PROC_SORT_KEY = "rss" if PROC_SORT_KEY == "cpu" else "cpu"
            last_refresh = 0  # 다음 루프에서 재정렬 반영

        elif ch == ord('f'):
            # 사용자 필터 입력
            prompt_y = h - 1
            name = prompt_input(stdscr, prompt_y, "필터: ", w)
            user_filter = (name or None)
            # 커서 리셋
            cursor = 0

        elif ch == ord('a'):
            # 사용자 필터 해제 (ALL)
            user_filter = None
            cursor = 0

        elif ch == ord('v'):
            # 타입 필터 순환: all -> ipynb -> python -> all
            type_filter = {"all":"ipynb", "ipynb":"python", "python":"all"}[type_filter]
            cursor = 0

        elif ch == ord('0'):
            type_filter = "all"; cursor = 0
        elif ch == ord('1'):
            type_filter = "ipynb"; cursor = 0
        elif ch == ord('2'):
            type_filter = "python"; cursor = 0    
        
        elif ch in (ord('t'), ord('x'), ord('i')):
            if not selected and 0 <= cursor < len(view):
                selected.add(view[cursor]["pid"])
            sig = signal.SIGTERM if ch==ord('t') else (signal.SIGKILL if ch==ord('x') else signal.SIGINT)
            # kill
            dead = []
            for pid in list(selected):
                try:
                    os.kill(pid, sig)
                    dead.append(pid)
                except ProcessLookupError:
                    dead.append(pid)  # 이미 종료
                except PermissionError:
                    # 권한 없음 표시
                    pass
                except Exception:
                    pass
            for pid in dead:
                selected.discard(pid)
            last_refresh = 0  # 바로 반영

def main():
    curses.wrapper(run)

if __name__ == "__main__":
    main()
