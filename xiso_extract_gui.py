#!/usr/bin/env python3
"""
xiso-extract-gui: Interface gráfica simples para extrair ISOs de Xbox
(formato XDVDFS) no Debian/Linux. Equivalente ao ExtractXISO, com GUI.

Requisitos:
    sudo apt install python3-tk   (caso o tkinter não esteja instalado)

Uso:
    python3 xiso_extract_gui.py
"""

import os
import struct
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SECTOR_SIZE = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
ATTR_DIRECTORY = 0x10

# O cabeçalho XDVDFS sempre fica no setor 32 (0x10000 bytes) dentro da
# partição de dados. Os deslocamentos abaixo são o INÍCIO DA PARTIÇÃO
# (não do cabeçalho), usados tanto para achar a assinatura (partição +
# 0x10000) quanto para toda a aritmética de setores dos arquivos.
# Mesmos valores usados pelo extract-xiso para Xbox original, XGD2 e
# XGD3 (Xbox 360).
HEADER_OFFSET = 0x10000
CANDIDATE_OFFSETS = [
    ("Xbox original", 0),
    ("Xbox 360 (XGD2)", 0x0FD90000),
    ("Xbox 360 (XGD3)", 0x02080000),
    ("Xbox (layout alternativo)", 0x18300000),
]


class XisoError(Exception):
    pass


class DirEntry:
    __slots__ = ("name", "start_sector", "size", "attributes", "is_dir")

    def __init__(self, name, start_sector, size, attributes):
        self.name = name
        self.start_sector = start_sector
        self.size = size
        self.attributes = attributes
        self.is_dir = bool(attributes & ATTR_DIRECTORY)


def read_volume_descriptor(f, header_location):
    f.seek(header_location + len(MAGIC))
    root_sector, root_size = struct.unpack("<II", f.read(8))
    return root_sector, root_size


def read_dir_table(f, base, sector, size):
    f.seek(base + sector * SECTOR_SIZE)
    return f.read(size)


def validate_root_dir(f, base, root_sector, root_size, file_size):
    """Confere se a tabela de diretório raiz apontada por este offset faz
    sentido, para descartar assinaturas 'decoy'. Retorna (valido, motivo,
    info_diagnostico)."""
    info = {"root_sector": root_sector, "root_size": root_size}
    if root_sector == 0 or root_size == 0:
        return False, "root_sector ou root_size igual a 0", info
    if root_size > 256 * 1024 * 1024:
        return False, f"root_size implausível ({root_size} bytes)", info
    data_start = base + root_sector * SECTOR_SIZE
    info["data_start"] = data_start
    if data_start + root_size > file_size + SECTOR_SIZE:
        return False, "tabela de diretório apontaria para fora do arquivo", info
    try:
        table = read_dir_table(f, base, root_sector, min(root_size, SECTOR_SIZE))
    except OSError:
        return False, "erro de leitura ao acessar a tabela", info
    info["primeiros_bytes"] = table[:32].hex(" ")
    if len(table) < 14:
        return False, "tabela menor que uma entrada", info
    left, right = struct.unpack_from("<HH", table, 0)
    if left == 0xFFFF and right == 0xFFFF:
        return False, "tabela vazia (0xFFFF/0xFFFF logo no início)", info
    start_sector, _file_size_field = struct.unpack_from("<II", table, 4)
    name_len = table[13]
    if name_len == 0:
        return False, "primeira entrada com nome de tamanho 0", info
    if name_len > 255 or 14 + name_len > len(table):
        return False, f"tamanho de nome inválido ({name_len})", info
    name_bytes = table[14:14 + name_len]
    if any(b < 0x20 or b > 0x7E for b in name_bytes):
        return False, f"nome com bytes não imprimíveis ({name_bytes!r})", info
    if base + start_sector * SECTOR_SIZE > file_size:
        return False, "primeiro arquivo apontaria para fora do arquivo", info
    info["primeiro_nome"] = name_bytes.decode("ascii", errors="replace")
    return True, "ok", info


def scan_for_magic(f, file_size, chunk_size=64 * 1024 * 1024):
    """Varre o arquivo inteiro à procura da assinatura XDVDFS, para os
    casos em que ela não está em nenhum dos offsets conhecidos."""
    overlap = len(MAGIC) - 1
    f.seek(0)
    offset = 0
    prev_tail = b""
    found = []
    while offset < file_size:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        buf = prev_tail + chunk
        search_from = 0
        while True:
            idx = buf.find(MAGIC, search_from)
            if idx == -1:
                break
            found.append(offset - len(prev_tail) + idx)
            search_from = idx + 1
        prev_tail = buf[-overlap:] if len(buf) >= overlap else buf
        offset += len(chunk)
    return found


def find_volume_base(f, log=lambda msg: None):
    """Procura a assinatura XDVDFS nos offsets conhecidos, validando a
    tabela de diretório antes de aceitar. Se nada bater, faz uma
    varredura completa do arquivo como último recurso."""
    f.seek(0, 2)
    file_size = f.tell()

    tried = []

    def try_candidate(label, base):
        """base = início da partição (usado na aritmética de setores).
        O cabeçalho XDVDFS fica sempre 32 setores (0x10000 bytes) à frente."""
        header_location = base + HEADER_OFFSET
        f.seek(header_location)
        if f.read(len(MAGIC)) != MAGIC:
            return None
        root_sector, root_size = read_volume_descriptor(f, header_location)
        ok, reason, info = validate_root_dir(f, base, root_sector, root_size, file_size)
        log(f"Assinatura encontrada em {label} (cabeçalho em {hex(header_location)}): "
            f"root_sector={info.get('root_sector')} root_size={info.get('root_size')} "
            f"-> {'válida' if ok else 'rejeitada: ' + reason}")
        if "primeiros_bytes" in info:
            log(f"  primeiros bytes da tabela: {info['primeiros_bytes']}")
        if ok:
            return base, label, root_sector, root_size
        tried.append(f"{label} (cabeçalho em {hex(header_location)}): {reason}")
        return None

    for label, base in CANDIDATE_OFFSETS:
        result = try_candidate(label, base)
        if result:
            return result

    log("Nenhum offset conhecido validou, varrendo o arquivo inteiro em busca "
        "da assinatura (pode demorar um pouco em ISOs grandes)...")
    known_header_locations = {base + HEADER_OFFSET for _, base in CANDIDATE_OFFSETS}
    for header_location in scan_for_magic(f, file_size):
        if header_location in known_header_locations:
            continue
        base = header_location - HEADER_OFFSET
        if base < 0:
            continue
        result = try_candidate("offset customizado", base)
        if result:
            return result

    detail = ""
    if tried:
        detail = " Detalhes:\n" + "\n".join(tried)
    raise XisoError(
        "Nenhuma tabela de diretório válida foi encontrada. O arquivo pode "
        "estar corrompido, incompleto, ou usar um layout não suportado." + detail
    )


def parse_dir_entries(table):
    entries = []
    pos = 0
    n = len(table)
    while pos + 14 <= n:
        left, right = struct.unpack_from("<HH", table, pos)
        if left == 0xFFFF and right == 0xFFFF:
            break
        start_sector, file_size = struct.unpack_from("<II", table, pos + 4)
        attributes = table[pos + 12]
        name_len = table[pos + 13]
        if name_len == 0:
            # preenchimento/fim de tabela com bytes zero, em vez de 0xFF
            break
        if pos + 14 + name_len > n:
            # entrada incompleta - provavelmente lixo/corrupção, parar aqui
            break
        name_bytes = table[pos + 14:pos + 14 + name_len]
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError:
            name = name_bytes.decode("latin-1")
        entries.append(DirEntry(name, start_sector, file_size, attributes))
        entry_size = 14 + name_len
        pos += (entry_size + 3) & ~3
    return entries


def walk(f, base, sector, size, prefix, extract_to, log):
    table = read_dir_table(f, base, sector, size)
    count = 0
    for entry in parse_dir_entries(table):
        rel_path = os.path.join(prefix, entry.name) if prefix else entry.name
        if entry.is_dir:
            os.makedirs(os.path.join(extract_to, rel_path), exist_ok=True)
            log(f"[DIR]  {rel_path}")
            if entry.size > 0:
                count += walk(f, base, entry.start_sector, entry.size,
                               rel_path, extract_to, log)
        else:
            log(f"[FILE] {rel_path}  ({entry.size} bytes)")
            extract_file(f, base, entry, rel_path, extract_to)
            count += 1
    return count


def extract_file(f, base, entry, rel_path, extract_to):
    out_path = os.path.join(extract_to, rel_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    f.seek(base + entry.start_sector * SECTOR_SIZE)
    remaining = entry.size
    with open(out_path, "wb") as out:
        chunk_size = 1024 * 1024
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                raise XisoError(f"Fim de arquivo inesperado ao extrair {rel_path}")
            out.write(chunk)
            remaining -= len(chunk)


def extract_iso(iso_path, output_dir, log):
    os.makedirs(output_dir, exist_ok=True)
    with open(iso_path, "rb") as f:
        base, label, root_sector, root_size = find_volume_base(f, log)
        log(f"ISO detectada: {label} (offset do volume: {hex(base)}).")
        log(f"Extraindo para: {os.path.abspath(output_dir)}")
        total = walk(f, base, root_sector, root_size, "", output_dir, log)
    return total


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Extrator de ISO Xbox")
        self.geometry("620x460")
        self.resizable(True, True)
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        frame_top = ttk.Frame(self)
        frame_top.pack(fill="x", **pad)

        # Caminho do .iso
        ttk.Label(frame_top, text="Arquivo .iso:").grid(row=0, column=0, sticky="w")
        self.iso_var = tk.StringVar()
        ttk.Entry(frame_top, textvariable=self.iso_var, width=60).grid(
            row=1, column=0, sticky="we", padx=(0, 6))
        ttk.Button(frame_top, text="Procurar...", command=self.choose_iso).grid(
            row=1, column=1)

        # Pasta de destino
        ttk.Label(frame_top, text="Pasta de destino:").grid(
            row=2, column=0, sticky="w", pady=(10, 0))
        self.output_var = tk.StringVar()
        ttk.Entry(frame_top, textvariable=self.output_var, width=60).grid(
            row=3, column=0, sticky="we", padx=(0, 6))
        ttk.Button(frame_top, text="Procurar...", command=self.choose_output).grid(
            row=3, column=1)

        frame_top.columnconfigure(0, weight=1)

        # Botão extrair + barra de progresso
        frame_actions = ttk.Frame(self)
        frame_actions.pack(fill="x", **pad)
        self.extract_btn = ttk.Button(
            frame_actions, text="Extrair", command=self.start_extraction)
        self.extract_btn.pack(side="left")
        self.progress = ttk.Progressbar(frame_actions, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Log
        frame_log = ttk.Frame(self)
        frame_log.pack(fill="both", expand=True, **pad)
        ttk.Label(frame_log, text="Log:").pack(anchor="w")
        self.log_text = tk.Text(frame_log, wrap="none", state="disabled")
        self.log_text.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(frame_log, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        # Status
        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=8, pady=(0, 8))

    def choose_iso(self):
        path = filedialog.askopenfilename(
            title="Selecione a imagem ISO do Xbox",
            filetypes=[("Imagens ISO", "*.iso"), ("Todos os arquivos", "*.*")],
        )
        if path:
            self.iso_var.set(path)
            if not self.output_var.get():
                default_out = os.path.splitext(path)[0]
                self.output_var.set(default_out)

    def choose_output(self):
        path = filedialog.askdirectory(title="Selecione a pasta de destino")
        if path:
            self.output_var.set(path)

    def log(self, message):
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(0, append)

    def start_extraction(self):
        iso_path = self.iso_var.get().strip()
        output_dir = self.output_var.get().strip()

        if not iso_path:
            messagebox.showerror("Erro", "Selecione o arquivo .iso.")
            return
        if not os.path.isfile(iso_path):
            messagebox.showerror("Erro", "O arquivo .iso informado não existe.")
            return
        if not output_dir:
            messagebox.showerror("Erro", "Selecione a pasta de destino.")
            return

        self.extract_btn.configure(state="disabled")
        self.status_var.set("Extraindo...")
        self.progress.start(10)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        thread = threading.Thread(
            target=self._run_extraction, args=(iso_path, output_dir), daemon=True
        )
        thread.start()

    def _run_extraction(self, iso_path, output_dir):
        try:
            total = extract_iso(iso_path, output_dir, self.log)
            self.after(0, self._on_success, total)
        except XisoError as e:
            self.after(0, self._on_error, str(e))
        except Exception as e:
            self.after(0, self._on_error, f"Erro inesperado: {e}")

    def _on_success(self, total):
        self.progress.stop()
        self.extract_btn.configure(state="normal")
        self.status_var.set(f"Concluído: {total} arquivo(s) extraído(s).")
        messagebox.showinfo("Concluído", f"Extração concluída: {total} arquivo(s).")

    def _on_error(self, message):
        self.progress.stop()
        self.extract_btn.configure(state="normal")
        self.status_var.set("Erro na extração.")
        messagebox.showerror("Erro", message)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
