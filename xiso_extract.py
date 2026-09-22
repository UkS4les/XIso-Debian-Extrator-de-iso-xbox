#!/usr/bin/env python3
"""
xiso-extract: Extrator de imagens ISO do Xbox (XDVDFS) para Linux/Debian.
Equivalente simplificado ao ExtractXISO (Windows), em Python puro
(sem dependências externas).

Uso:
    python3 xiso_extract.py imagem.iso [-o pasta_saida] [-l] [-v]

Opções:
    -o, --output   Pasta de destino (padrão: nome do ISO sem extensão)
    -l, --list     Apenas lista o conteúdo, sem extrair
    -v, --verbose  Mostra cada arquivo/pasta durante o processamento
"""

import argparse
import os
import struct
import sys

SECTOR_SIZE = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
ATTR_DIRECTORY = 0x10

HEADER_OFFSET = 0x10000  # o cabeçalho XDVDFS sempre fica no setor 32 dentro da partição
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
    sentido, para descartar assinaturas 'decoy' que alguns discos de
    Xbox 360 têm fora da posição real dos dados do jogo."""
    if root_sector == 0 or root_size == 0:
        return False, "root_sector ou root_size igual a 0", {"root_sector": root_sector, "root_size": root_size}
    info = {"root_sector": root_sector, "root_size": root_size}
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


def find_volume_base(f, log=print):
    """Procura a assinatura XDVDFS nos offsets conhecidos (Xbox original,
    Xbox 360 XGD2, Xbox 360 XGD3, layout alternativo, offset 0), validando
    a tabela de diretório antes de aceitar. Se nada bater, faz uma
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
    """A tabela de diretório é uma árvore binária serializada, mas para
    extrair todos os arquivos basta varrer sequencialmente cada entrada
    até encontrar o marcador de fim (0xFFFF/0xFFFF)."""
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
            break
        if pos + 14 + name_len > n:
            break
        name_bytes = table[pos + 14:pos + 14 + name_len]
        try:
            name = name_bytes.decode("ascii")
        except UnicodeDecodeError:
            name = name_bytes.decode("latin-1")
        entries.append(DirEntry(name, start_sector, file_size, attributes))
        entry_size = 14 + name_len
        pos += (entry_size + 3) & ~3  # alinhamento de 4 bytes
    return entries


def walk(f, base, sector, size, prefix, listing, extract_to, verbose):
    table = read_dir_table(f, base, sector, size)
    for entry in parse_dir_entries(table):
        rel_path = os.path.join(prefix, entry.name) if prefix else entry.name
        if entry.is_dir:
            listing.append(rel_path + "/")
            if extract_to is not None:
                os.makedirs(os.path.join(extract_to, rel_path), exist_ok=True)
            if verbose:
                print(f"  [DIR]  {rel_path}")
            if entry.size > 0:
                walk(f, base, entry.start_sector, entry.size,
                     rel_path, listing, extract_to, verbose)
        else:
            listing.append(f"{rel_path}  ({entry.size} bytes)")
            if verbose:
                print(f"  [FILE] {rel_path}  ({entry.size} bytes)")
            if extract_to is not None:
                extract_file(f, base, entry, rel_path, extract_to)


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


def main():
    parser = argparse.ArgumentParser(
        description="Extrator de imagens ISO do Xbox (formato XDVDFS), "
                     "equivalente ao ExtractXISO."
    )
    parser.add_argument("iso", help="Caminho para o arquivo .iso do Xbox")
    parser.add_argument("-o", "--output", help="Pasta de destino para os arquivos extraídos")
    parser.add_argument("-l", "--list", action="store_true",
                         help="Apenas lista o conteúdo, sem extrair")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Mostra cada arquivo/pasta durante o processamento")
    args = parser.parse_args()

    if not os.path.isfile(args.iso):
        print(f"Erro: arquivo não encontrado: {args.iso}", file=sys.stderr)
        sys.exit(1)

    extract_to = None
    if not args.list:
        extract_to = args.output or os.path.splitext(os.path.basename(args.iso))[0]
        os.makedirs(extract_to, exist_ok=True)

    try:
        with open(args.iso, "rb") as f:
            base, label, root_sector, root_size = find_volume_base(f)
            listing = []
            print(f"ISO detectada: {label} (offset do volume: {hex(base)}).")
            if extract_to:
                print(f"Extraindo para: {os.path.abspath(extract_to)}\n")
            else:
                print("Listando conteúdo:\n")
            walk(f, base, root_sector, root_size, "", listing, extract_to, args.verbose)
    except XisoError as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nConcluído: {len(listing)} itens processados.")
    if not args.verbose:
        for item in listing:
            print(" ", item)


if __name__ == "__main__":
    main()
