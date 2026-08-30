# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import math
import time
import shutil
import queue
import tempfile
import threading
import subprocess
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

AUDIO_EXTS = {".mp3",".wav",".m4a",".aac",".flac",".ogg",".opus",".wma"}

# V10: blocos pequenos para progresso visível
CHUNK_SECONDS = 60

# watchdog por bloco: se 1 minuto de áudio demorar mais que isso, considera travado
CHUNK_TIMEOUT_SECONDS = 600  # 10 minutos
CHUNK_MAX_RETRIES = 3

def downloads_folder():
    home = Path.home()
    for p in [
        home/"Downloads",
        home/"Download",
        Path(os.environ.get("USERPROFILE", str(home)))/"Downloads",
        Path(os.environ.get("USERPROFILE", str(home)))/"Download",
    ]:
        if p.exists() and p.is_dir():
            return p
    return Path.cwd()

def fmt_time(seconds):
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def now():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")

def parse_whisper_line(line):
    m = re.search(
        r"\[(\d\d):(\d\d):(\d\d)\.(\d+)\s*-->\s*"
        r"(\d\d):(\d\d):(\d\d)\.(\d+)\]\s*(.*)",
        line
    )
    if not m:
        return None

    h1,m1,s1,ms1,h2,m2,s2,ms2,text = m.groups()
    start = int(h1)*3600 + int(m1)*60 + int(s1) + int(ms1[:3].ljust(3,"0"))/1000
    end   = int(h2)*3600 + int(m2)*60 + int(s2) + int(ms2[:3].ljust(3,"0"))/1000
    text = re.sub(r"\s+"," ",text).strip()

    if not text:
        return None

    return {"start": start, "end": end, "text": text}

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Transcritor de Áudio para PDF - V10 AUDITORIA")
        self.geometry("1180x880")
        self.minsize(1000, 740)

        self.base = Path(__file__).resolve().parent
        self.downloads = downloads_folder()
        self.models_dir = self.downloads / "TRANSCRITOR_MODELOS"
        self.models_dir.mkdir(exist_ok=True)

        self.audio_files = []
        self.current_audio = None
        self.proc = None
        self.records = []
        self.last_pdf = None
        self.q = queue.Queue()
        self.cancel_requested = False

        self.current_chunk_no = 0
        self.total_chunks = 0
        self.chunk_started_at = None
        self.process_pid = None
        self.retry_no = 0
        self.worker_started_at = None
        self.last_event_at = time.time()

        self.build_ui()
        self.refresh_files()

        self.after(200, self.poll_queue)
        self.after(1000, self.heartbeat_ui)

    # ---------------- UI ----------------
    def build_ui(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        ttk.Label(
            top,
            text="TRANSCRITOR DE ÁUDIO PARA PDF - V10 AUDITORIA",
            font=("Segoe UI", 18, "bold")
        ).pack(anchor="w")

        ttk.Label(
            top,
            text=f"Áudios e saídas: {self.downloads}"
        ).pack(anchor="w", pady=(3,0))

        ttk.Label(
            top,
            text=f"Modelos: {self.models_dir}"
        ).pack(anchor="w", pady=(2,0))

        box = ttk.LabelFrame(self, text="Áudio", padding=10)
        box.pack(fill="x", padx=12, pady=(0,8))

        self.file_combo = ttk.Combobox(box, state="readonly", width=86)
        self.file_combo.grid(row=0, column=0, sticky="ew", padx=(0,8))
        self.file_combo.bind("<<ComboboxSelected>>", self.select_file)

        ttk.Button(
            box, text="Atualizar lista", command=self.refresh_files
        ).grid(row=0, column=1)

        box.columnconfigure(0, weight=1)

        opts = ttk.LabelFrame(self, text="Configuração", padding=10)
        opts.pack(fill="x", padx=12, pady=(0,8))

        ttk.Label(opts, text="Modelo:").grid(row=0, column=0, sticky="w")
        self.model_var = tk.StringVar(value="small")
        self.model_combo = ttk.Combobox(
            opts,
            state="readonly",
            textvariable=self.model_var,
            values=["small","base"],
            width=14
        )
        self.model_combo.grid(row=0, column=1, padx=(8,16), sticky="w")

        ttk.Label(
            opts,
            text="V10 usa blocos de 1 minuto para progresso frequente."
        ).grid(row=0, column=2, sticky="w")

        btns = ttk.Frame(self, padding=(12,2))
        btns.pack(fill="x")

        self.start_btn = ttk.Button(
            btns,
            text="INICIAR / RETOMAR",
            command=self.start
        )
        self.start_btn.pack(side="left")

        ttk.Button(
            btns,
            text="PARAR",
            command=self.stop
        ).pack(side="left", padx=8)

        ttk.Button(
            btns,
            text="ABRIR PDF",
            command=self.open_pdf
        ).pack(side="right")

        # Painel auditoria
        audit = ttk.LabelFrame(self, text="AUDITORIA EM TEMPO REAL", padding=10)
        audit.pack(fill="x", padx=12, pady=(0,8))

        grid = ttk.Frame(audit)
        grid.pack(fill="x")

        self.stage_var = tk.StringVar(value="Aguardando")
        self.block_var = tk.StringVar(value="-")
        self.pid_var = tk.StringVar(value="-")
        self.retry_var = tk.StringVar(value="0")
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.chunk_elapsed_var = tk.StringVar(value="00:00:00")
        self.alive_var = tk.StringVar(value="PARADO")
        self.last_event_var = tk.StringVar(value="-")

        labels = [
            ("Etapa:", self.stage_var),
            ("Bloco:", self.block_var),
            ("PID:", self.pid_var),
            ("Tentativa:", self.retry_var),
            ("Tempo total:", self.elapsed_var),
            ("Tempo bloco:", self.chunk_elapsed_var),
            ("Processo:", self.alive_var),
            ("Último evento:", self.last_event_var),
        ]

        for i, (lab, var) in enumerate(labels):
            r = i // 4
            c = (i % 4) * 2
            ttk.Label(grid, text=lab, font=("Segoe UI",9,"bold")).grid(
                row=r, column=c, sticky="w", padx=(0,4), pady=2
            )
            ttk.Label(grid, textvariable=var).grid(
                row=r, column=c+1, sticky="w", padx=(0,18), pady=2
            )

        # Download progress
        status_frame = ttk.Frame(self, padding=(12,6))
        status_frame.pack(fill="x")

        self.status = tk.StringVar(value="Pronto.")
        ttk.Label(
            status_frame,
            textvariable=self.status,
            font=("Segoe UI",10,"bold")
        ).pack(anchor="w")

        ttk.Label(status_frame, text="Download do modelo").pack(anchor="w", pady=(6,0))
        self.download_progress = ttk.Progressbar(
            status_frame, mode="determinate", maximum=100
        )
        self.download_progress.pack(fill="x")
        self.download_label = tk.StringVar(value="0.0%")
        ttk.Label(status_frame, textvariable=self.download_label).pack(anchor="e")

        ttk.Label(status_frame, text="Transcrição geral").pack(anchor="w", pady=(6,0))
        self.progress = ttk.Progressbar(
            status_frame, mode="determinate", maximum=100
        )
        self.progress.pack(fill="x")
        self.progress_label = tk.StringVar(value="0.0%")
        ttk.Label(status_frame, textvariable=self.progress_label).pack(anchor="e")

        # current chunk animated heartbeat bar
        ttk.Label(status_frame, text="Atividade do bloco atual").pack(anchor="w", pady=(6,0))
        self.activity = ttk.Progressbar(
            status_frame, mode="indeterminate"
        )
        self.activity.pack(fill="x")

        fr = ttk.LabelFrame(
            self,
            text="Afirmações reconhecidas em tempo real",
            padding=8
        )
        fr.pack(fill="both", expand=True, padx=12, pady=(0,10))

        self.text = tk.Text(fr, wrap="word", font=("Segoe UI",11))
        self.text.pack(side="left", fill="both", expand=True)

        sc = ttk.Scrollbar(fr, command=self.text.yview)
        sc.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=sc.set)

        ttk.Label(
            self,
            text=(
                "V10 grava AUDITORIA_V10.log, estado de retomada, progresso e PDF em Downloads. "
                "Watchdog reinicia bloco travado automaticamente."
            ),
            padding=(12,0,12,12)
        ).pack(anchor="w")

    def append(self, s):
        self.text.insert("end", s + "\n")
        self.text.see("end")

    # ---------------- basic paths ----------------
    def audit_path(self):
        if self.current_audio:
            return self.current_audio.with_name(
                self.current_audio.stem + "_AUDITORIA_V10.log"
            )
        return self.downloads / "AUDITORIA_V10.log"

    def audit_log(self, msg):
        line = f"[{now()}] {msg}"
        try:
            with open(self.audit_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except:
            pass
        self.q.put(("audit_event", line))

    def refresh_files(self):
        self.audio_files = sorted(
            [
                p for p in self.downloads.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS
            ],
            key=lambda p: p.name.lower()
        )
        names = [p.name for p in self.audio_files]
        self.file_combo["values"] = names

        if names:
            self.file_combo.current(0)
            self.current_audio = self.audio_files[0]
            self.status.set(f"{len(names)} áudio(s) encontrado(s).")
        else:
            self.current_audio = None
            self.status.set("Nenhum áudio encontrado em Downloads.")

    def select_file(self, event=None):
        i = self.file_combo.current()
        if 0 <= i < len(self.audio_files):
            self.current_audio = self.audio_files[i]
            self.status.set("Selecionado: " + self.current_audio.name)

    # ---------------- model download ----------------
    def model_url(self, model):
        return f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model}.bin"

    def model_path(self, model):
        return self.models_dir / f"ggml-{model}.bin"

    def discover_total_size(self, session, url):
        headers = {"Range": "bytes=0-0"}
        r = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=(20,60),
            allow_redirects=True
        )
        try:
            cr = r.headers.get("Content-Range","")
            m = re.search(r"/(\d+)$", cr)
            if m:
                return int(m.group(1))
            cl = r.headers.get("Content-Length")
            if cl:
                return int(cl)
            return None
        finally:
            r.close()

    def download_model_resumable(self, model):
        import requests

        self.q.put(("stage","DOWNLOAD MODELO"))
        self.audit_log(f"Iniciando verificação/download do modelo {model}")

        url = self.model_url(model)
        dest = self.model_path(model)
        part = dest.with_suffix(dest.suffix + ".part")

        session = requests.Session()
        session.headers.update({"User-Agent":"TranscritorV10/1.0"})

        total = None
        for attempt in range(1,4):
            try:
                total = self.discover_total_size(session,url)
                if total:
                    break
            except Exception as e:
                self.audit_log(f"Falha ao descobrir tamanho tentativa {attempt}: {e}")
                time.sleep(2)

        if dest.exists():
            if total and dest.stat().st_size == total:
                self.q.put(("download_progress",100.0))
                self.audit_log(f"Modelo completo já existe: {dest}")
                return dest
            if not total and dest.stat().st_size > 10_000_000:
                self.q.put(("download_progress",100.0))
                self.audit_log(f"Modelo existente reutilizado sem tamanho remoto: {dest}")
                return dest

        max_retries = 25

        for attempt in range(1, max_retries+1):
            if self.cancel_requested:
                raise RuntimeError("Cancelado pelo usuário.")

            existing = part.stat().st_size if part.exists() else 0
            headers = {}
            mode = "wb"

            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
                mode = "ab"
                self.audit_log(
                    f"Retomando download tentativa {attempt}, byte {existing}"
                )
            else:
                self.audit_log(f"Iniciando download tentativa {attempt}")

            try:
                r = session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(20,60),
                    allow_redirects=True
                )

                if existing > 0 and r.status_code == 200:
                    self.audit_log("Servidor ignorou Range; reiniciando parcial corretamente")
                    r.close()
                    part.unlink(missing_ok=True)
                    existing = 0
                    mode = "wb"
                    r = session.get(
                        url,
                        stream=True,
                        timeout=(20,60),
                        allow_redirects=True
                    )

                if r.status_code not in (200,206):
                    raise RuntimeError(f"HTTP {r.status_code}")

                cr = r.headers.get("Content-Range","")
                m = re.search(r"/(\d+)$", cr)
                if m:
                    total = int(m.group(1))
                elif not total:
                    cl = r.headers.get("Content-Length")
                    if cl:
                        size_here = int(cl)
                        total = existing + size_here if r.status_code == 206 else size_here

                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if self.cancel_requested:
                            raise RuntimeError("Cancelado pelo usuário.")
                        if not chunk:
                            continue
                        f.write(chunk)
                        f.flush()

                        current = part.stat().st_size
                        if total:
                            pct = min(100.0, current*100.0/total)
                            self.q.put(("download_progress",pct))
                            self.q.put((
                                "status",
                                f"Modelo {model}: {pct:.1f}% "
                                f"({current/1024/1024:.1f}/{total/1024/1024:.1f} MB)"
                            ))
                        else:
                            self.q.put((
                                "status",
                                f"Modelo {model}: {current/1024/1024:.1f} MB"
                            ))

                r.close()

                current = part.stat().st_size
                if total and current != total:
                    raise RuntimeError(f"Download incompleto {current}/{total}")

                if dest.exists():
                    dest.unlink()
                part.rename(dest)

                self.q.put(("download_progress",100.0))
                self.audit_log(f"Download concluído: {dest} ({dest.stat().st_size} bytes)")
                return dest

            except Exception as e:
                current = part.stat().st_size if part.exists() else 0
                self.audit_log(
                    f"Download falhou tentativa {attempt}/{max_retries}: {e}. "
                    f"Parcial={current} bytes"
                )
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"Download falhou após {max_retries} tentativas. "
                        f"Parcial preservado em {part}"
                    )
                wait = min(30, 2 + attempt*2)
                self.q.put((
                    "status",
                    f"Internet oscilou. Parcial salvo. Retomando em {wait}s..."
                ))
                time.sleep(wait)

    # ---------------- whisper/ffmpeg ----------------
    def find_whisper_cli(self):
        found = list((self.base/"whisper").rglob("whisper-cli.exe"))
        if not found:
            raise FileNotFoundError("whisper-cli.exe não encontrado no pacote.")
        return found[0]

    def preflight(self, ffmpeg, cli):
        self.q.put(("stage","PRÉ-TESTES"))
        self.audit_log("Iniciando pré-testes")

        # ffmpeg
        r = subprocess.run(
            [ffmpeg, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20
        )
        if r.returncode != 0:
            raise RuntimeError("FFmpeg não abriu.")
        self.audit_log("FFmpeg OK")

        # whisper-cli
        r = subprocess.run(
            [str(cli), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20
        )
        if r.returncode != 0:
            raise RuntimeError(
                "whisper-cli.exe não abriu no pré-teste.\n" + r.stdout[-1500:]
            )
        self.audit_log("whisper-cli OK")

        # disk
        usage = shutil.disk_usage(self.downloads)
        free_gb = usage.free / (1024**3)
        self.audit_log(f"Espaço livre em Downloads: {free_gb:.2f} GB")
        if free_gb < 2:
            raise RuntimeError(
                f"Pouco espaço em disco: apenas {free_gb:.2f} GB livres."
            )

    def get_duration(self, ffmpeg, audio):
        self.q.put(("stage","LENDO DURAÇÃO"))
        r = subprocess.run(
            [ffmpeg, "-i", str(audio)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        m = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            r.stderr
        )
        if not m:
            raise RuntimeError("Não consegui descobrir a duração do áudio.")
        h,mn,s = m.groups()
        duration = int(h)*3600 + int(mn)*60 + float(s)
        self.audit_log(f"Duração detectada: {duration:.2f}s")
        return duration

    # ---------------- state ----------------
    def state_path(self):
        return self.current_audio.with_name(
            self.current_audio.stem + "_V10_ESTADO.json"
        )

    def load_state(self, model, total_chunks):
        default = {
            "audio": str(self.current_audio),
            "model": model,
            "chunk_seconds": CHUNK_SECONDS,
            "total_chunks": total_chunks,
            "completed_chunks": [],
            "records": []
        }

        p = self.state_path()
        if not p.exists():
            return default

        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            compatible = (
                state.get("audio") == str(self.current_audio)
                and state.get("model") == model
                and state.get("chunk_seconds") == CHUNK_SECONDS
                and state.get("total_chunks") == total_chunks
            )
            if compatible:
                return state
        except:
            pass

        return default

    def save_state(self, state):
        tmp = self.state_path().with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        os.replace(tmp, self.state_path())

    # ---------------- controls ----------------
    def start(self):
        if self.worker_started_at and self.start_btn["state"] == "disabled":
            messagebox.showinfo("Transcrição","Já está rodando.")
            return

        if not self.current_audio:
            messagebox.showwarning("Áudio","Escolha um áudio.")
            return

        self.cancel_requested = False
        self.last_pdf = None
        self.worker_started_at = time.time()
        self.chunk_started_at = None
        self.process_pid = None
        self.retry_no = 0

        self.start_btn.configure(state="disabled")
        self.activity.start(12)

        self.audit_log("="*70)
        self.audit_log("INÍCIO V10")
        self.audit_log(f"Áudio: {self.current_audio}")
        self.audit_log(f"Modelo: {self.model_var.get()}")

        threading.Thread(target=self.worker, daemon=True).start()

    def stop(self):
        self.cancel_requested = True
        self.audit_log("PARADA solicitada pelo usuário")

        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.audit_log("Processo whisper atual recebeu terminate()")
            except Exception as e:
                self.audit_log(f"Falha ao terminar processo: {e}")

        self.status.set("Parando... tudo concluído permanece salvo.")

    # ---------------- worker ----------------
    def worker(self):
        tmpdir = None
        try:
            import imageio_ffmpeg

            self.q.put(("stage","INICIALIZANDO"))
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            cli = self.find_whisper_cli()

            self.preflight(ffmpeg, cli)

            model_name = self.model_var.get()
            model = self.download_model_resumable(model_name)

            duration = self.get_duration(ffmpeg, self.current_audio)
            total_chunks = math.ceil(duration / CHUNK_SECONDS)
            self.total_chunks = total_chunks

            state = self.load_state(model_name, total_chunks)
            completed = set(state.get("completed_chunks", []))
            self.records = state.get("records", [])

            self.audit_log(
                f"Total de blocos: {total_chunks}; já concluídos: {len(completed)}"
            )

            self.q.put(("clear",None))

            if self.records:
                for r in self.records:
                    self.q.put((
                        "segment",
                        f"[{fmt_time(r['start'])} - {fmt_time(r['end'])}] {r['text']}"
                    ))

            self.q.put(("progress",(len(completed)/total_chunks)*100.0))

            tmpdir = Path(tempfile.mkdtemp(prefix="transcritor_v10_"))

            whisper_log = self.current_audio.with_name(
                self.current_audio.stem + "_WHISPERCPP_V10.log"
            )
            progress_txt = self.current_audio.with_name(
                self.current_audio.stem + "_PROGRESSO_TRANSCRICAO.txt"
            )

            for idx in range(total_chunks):
                if self.cancel_requested:
                    self.audit_log("Worker encerrado por cancelamento")
                    return

                chunk_no = idx + 1
                if chunk_no in completed:
                    continue

                start_sec = idx * CHUNK_SECONDS
                remain = max(0, duration - start_sec)
                this_len = min(CHUNK_SECONDS, remain)

                self.current_chunk_no = chunk_no
                self.chunk_started_at = time.time()

                block_label = (
                    f"{chunk_no}/{total_chunks} "
                    f"({fmt_time(start_sec)} a {fmt_time(start_sec+this_len)})"
                )
                self.q.put(("block",block_label))
                self.q.put(("stage","CONVERTENDO BLOCO"))
                self.audit_log(f"Iniciando bloco {block_label}")

                wav = tmpdir / f"chunk_{chunk_no:04d}.wav"

                c = subprocess.run(
                    [
                        ffmpeg, "-y",
                        "-ss", str(start_sec),
                        "-t", str(this_len),
                        "-i", str(self.current_audio),
                        "-ar","16000",
                        "-ac","1",
                        "-c:a","pcm_s16le",
                        str(wav)
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )

                if c.returncode != 0 or not wav.exists():
                    raise RuntimeError(
                        f"Falha ao converter bloco {chunk_no}.\n{c.stderr[-2000:]}"
                    )

                self.audit_log(
                    f"Bloco {chunk_no}: WAV criado ({wav.stat().st_size} bytes)"
                )

                success = False
                last_error = None

                for attempt in range(1, CHUNK_MAX_RETRIES+1):
                    if self.cancel_requested:
                        return

                    self.retry_no = attempt
                    self.q.put(("retry",str(attempt)))
                    self.q.put(("stage","WHISPER PROCESSANDO"))
                    self.audit_log(
                        f"Bloco {chunk_no}: tentativa {attempt}/{CHUNK_MAX_RETRIES}"
                    )

                    cmd = [
                        str(cli),
                        "-m", str(model),
                        "-f", str(wav),
                        "-l", "pt",
                        "-nt"
                    ]

                    flags = subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0

                    with open(whisper_log, "a", encoding="utf-8") as log:
                        log.write(
                            f"\n===== BLOCO {chunk_no}/{total_chunks} "
                            f"TENTATIVA {attempt} {now()} =====\n"
                        )
                        log.flush()

                        self.proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            bufsize=1,
                            creationflags=flags
                        )

                        self.process_pid = self.proc.pid
                        self.q.put(("pid",str(self.proc.pid)))
                        self.audit_log(
                            f"Bloco {chunk_no}: whisper PID={self.proc.pid}"
                        )

                        lines_queue = queue.Queue()

                        def reader():
                            try:
                                for line in self.proc.stdout:
                                    lines_queue.put(line)
                            finally:
                                lines_queue.put(None)

                        rt = threading.Thread(target=reader, daemon=True)
                        rt.start()

                        block_records = []
                        reader_done = False
                        started = time.time()

                        while True:
                            if self.cancel_requested:
                                try:
                                    self.proc.terminate()
                                except:
                                    pass
                                return

                            elapsed = time.time() - started

                            if elapsed > CHUNK_TIMEOUT_SECONDS:
                                self.audit_log(
                                    f"WATCHDOG: bloco {chunk_no} excedeu "
                                    f"{CHUNK_TIMEOUT_SECONDS}s; matando PID {self.proc.pid}"
                                )
                                try:
                                    self.proc.kill()
                                except:
                                    pass
                                last_error = (
                                    f"Watchdog: bloco {chunk_no} excedeu "
                                    f"{CHUNK_TIMEOUT_SECONDS//60} minutos."
                                )
                                break

                            try:
                                item = lines_queue.get(timeout=1.0)
                                if item is None:
                                    reader_done = True
                                else:
                                    line = item.rstrip()
                                    log.write(line + "\n")
                                    log.flush()
                                    self.last_event_at = time.time()
                                    self.q.put(("last_event",now()))

                                    rec = parse_whisper_line(line)
                                    if rec:
                                        rec["start"] += start_sec
                                        rec["end"] += start_sec
                                        block_records.append(rec)
                                        self.q.put((
                                            "segment",
                                            f"[{fmt_time(rec['start'])} - "
                                            f"{fmt_time(rec['end'])}] {rec['text']}"
                                        ))
                            except queue.Empty:
                                # heartbeat: processo continua vivo
                                self.q.put((
                                    "status",
                                    f"Bloco {chunk_no}/{total_chunks} ativo — "
                                    f"{int(elapsed)}s processando — PID {self.proc.pid}"
                                ))

                            if reader_done and self.proc.poll() is not None:
                                break

                        if self.proc.poll() is None:
                            try:
                                self.proc.wait(timeout=5)
                            except:
                                pass

                        code = self.proc.returncode

                    if last_error and "Watchdog" in str(last_error):
                        self.audit_log(
                            f"Bloco {chunk_no}: tentativa {attempt} terminou por watchdog"
                        )
                        time.sleep(2)
                        continue

                    if code == 0:
                        self.audit_log(
                            f"Bloco {chunk_no}: tentativa {attempt} OK, "
                            f"{len(block_records)} segmentos"
                        )
                        success = True
                        break
                    else:
                        last_error = f"whisper.cpp retornou código {code}"
                        self.audit_log(
                            f"Bloco {chunk_no}: tentativa {attempt} falhou: {last_error}"
                        )
                        time.sleep(2)

                self.proc = None
                self.process_pid = None
                self.q.put(("pid","-"))

                if not success:
                    raise RuntimeError(
                        f"Bloco {chunk_no} falhou após {CHUNK_MAX_RETRIES} tentativas.\n"
                        f"Último erro: {last_error}\n"
                        f"Veja {whisper_log}"
                    )

                # só grava no estado após bloco 100% OK
                self.records.extend(block_records)
                completed.add(chunk_no)

                state["completed_chunks"] = sorted(completed)
                state["records"] = self.records
                self.save_state(state)

                with open(progress_txt,"w",encoding="utf-8") as f:
                    for r in self.records:
                        f.write(
                            f"[{fmt_time(r['start'])} - "
                            f"{fmt_time(r['end'])}] {r['text']}\n"
                        )

                pct = (len(completed)/total_chunks)*100.0
                self.q.put(("progress",pct))
                self.audit_log(
                    f"Bloco {chunk_no} CONCLUÍDO. Progresso geral {pct:.2f}%"
                )

                try:
                    wav.unlink()
                except:
                    pass

            if not self.records:
                raise RuntimeError("Nenhuma fala foi reconhecida.")

            self.q.put(("stage","GERANDO TXT/PDF"))
            self.audit_log("Gerando TXT e PDF final")

            txt = self.write_txt()
            pdf = self.write_pdf()
            self.last_pdf = pdf

            state["finished"] = True
            state["finished_at"] = datetime.now().isoformat()
            self.save_state(state)

            self.q.put(("progress",100.0))
            self.q.put(("done",(txt,pdf)))
            self.audit_log(f"FINALIZADO COM SUCESSO. PDF={pdf}")

        except Exception as e:
            self.audit_log(f"ERRO FATAL: {e}")
            self.q.put(("error",str(e)))

        finally:
            self.proc = None
            self.process_pid = None
            self.q.put(("pid","-"))
            self.q.put(("finish",None))
            if tmpdir:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except:
                    pass

    # ---------------- UI polling ----------------
    def heartbeat_ui(self):
        if self.worker_started_at:
            self.elapsed_var.set(
                fmt_time(time.time() - self.worker_started_at)
            )
        else:
            self.elapsed_var.set("00:00:00")

        if self.chunk_started_at:
            self.chunk_elapsed_var.set(
                fmt_time(time.time() - self.chunk_started_at)
            )
        else:
            self.chunk_elapsed_var.set("00:00:00")

        if self.proc and self.proc.poll() is None:
            self.alive_var.set("VIVO ✓")
            self.pid_var.set(str(self.proc.pid))
        else:
            if self.start_btn["state"] == "disabled":
                self.alive_var.set("TRABALHANDO")
            else:
                self.alive_var.set("PARADO")

        self.after(1000, self.heartbeat_ui)

    def poll_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()

                if kind == "clear":
                    self.text.delete("1.0","end")

                elif kind == "status":
                    self.status.set(data)

                elif kind == "segment":
                    self.append(data)

                elif kind == "stage":
                    self.stage_var.set(data)

                elif kind == "block":
                    self.block_var.set(data)

                elif kind == "pid":
                    self.pid_var.set(data)

                elif kind == "retry":
                    self.retry_var.set(data)

                elif kind == "last_event":
                    self.last_event_var.set(data)

                elif kind == "audit_event":
                    self.last_event_var.set(now())

                elif kind == "download_progress":
                    v = max(0,min(100,float(data)))
                    self.download_progress["value"] = v
                    self.download_label.set(f"{v:.1f}%")

                elif kind == "progress":
                    v = max(0,min(100,float(data)))
                    self.progress["value"] = v
                    self.progress_label.set(f"{v:.1f}%")

                elif kind == "done":
                    txt,pdf = data
                    self.stage_var.set("CONCLUÍDO")
                    self.status.set(f"Concluído. PDF: {pdf.name}")
                    self.append("\n=== PDF GERADO COM SUCESSO ===")
                    messagebox.showinfo(
                        "Concluído",
                        f"TXT:\n{txt}\n\nPDF:\n{pdf}"
                    )

                elif kind == "error":
                    self.stage_var.set("ERRO")
                    self.status.set(
                        "Erro. Auditoria e progresso foram preservados."
                    )
                    self.append("\n=== ERRO ===\n"+data)
                    messagebox.showerror(
                        "Erro",
                        data + "\n\nVeja também o arquivo AUDITORIA_V10.log em Downloads."
                    )

                elif kind == "finish":
                    self.start_btn.configure(state="normal")
                    self.activity.stop()
                    self.worker_started_at = None
                    self.chunk_started_at = None
                    self.retry_no = 0

        except queue.Empty:
            pass

        self.after(200,self.poll_queue)

    # ---------------- outputs ----------------
    def write_txt(self):
        audio = self.current_audio
        path = audio.with_name(audio.stem + "_AFIRMACOES_COMPLETAS.txt")
        with open(path,"w",encoding="utf-8") as f:
            f.write("AFIRMAÇÕES TRANSCRITAS\n")
            f.write("="*80+"\n\n")
            for r in self.records:
                f.write(r["text"]+"\n")
            f.write("\n\nTRANSCRIÇÃO COM HORÁRIO\n")
            f.write("="*80+"\n\n")
            for r in self.records:
                f.write(
                    f"[{fmt_time(r['start'])} - {fmt_time(r['end'])}] "
                    f"{r['text']}\n"
                )
        return path

    def write_pdf(self):
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, PageBreak
        )
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib.enums import TA_CENTER
        from xml.sax.saxutils import escape

        audio = self.current_audio
        path = audio.with_name(audio.stem + "_AFIRMACOES_TRANSCRITAS.pdf")

        doc = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            leftMargin=1.7*cm,
            rightMargin=1.7*cm,
            topMargin=1.7*cm,
            bottomMargin=1.7*cm
        )

        s = getSampleStyleSheet()

        title = ParagraphStyle(
            "t",
            parent=s["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            alignment=TA_CENTER,
            spaceAfter=14
        )

        body = ParagraphStyle(
            "b",
            parent=s["BodyText"],
            fontName="Helvetica",
            fontSize=11,
            leading=16,
            spaceAfter=7
        )

        small = ParagraphStyle(
            "sm",
            parent=s["BodyText"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            spaceAfter=4
        )

        story = [
            Paragraph("AFIRMAÇÕES TRANSCRITAS DO ÁUDIO",title),
            Paragraph(f"<b>Arquivo:</b> {escape(audio.name)}",body),
            Paragraph("<b>Motor:</b> whisper.cpp — V10 Auditada",body),
            Paragraph(
                f"<b>Gerado em:</b> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
                body
            ),
            Spacer(1,10)
        ]

        for r in self.records:
            story.append(Paragraph(escape(r["text"]),body))

        story.append(PageBreak())
        story.append(Paragraph("TRANSCRIÇÃO COM HORÁRIO",title))

        for r in self.records:
            story.append(
                Paragraph(
                    escape(
                        f"[{fmt_time(r['start'])} - {fmt_time(r['end'])}] "
                        f"{r['text']}"
                    ),
                    small
                )
            )

        doc.build(story)
        return path

    def open_pdf(self):
        pdf = self.last_pdf

        if not pdf and self.current_audio:
            candidate = self.current_audio.with_name(
                self.current_audio.stem + "_AFIRMACOES_TRANSCRITAS.pdf"
            )
            if candidate.exists():
                pdf = candidate

        if not pdf:
            messagebox.showinfo("PDF","O PDF ainda não foi gerado.")
            return

        os.startfile(str(pdf))

if __name__ == "__main__":
    App().mainloop()
