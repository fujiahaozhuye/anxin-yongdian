# -*- coding: utf-8 -*-
"""
纯 Python 二维码生成器（字节模式，版本 1～10，纠错等级 L/M/Q/H）
------------------------------------------------------------
为什么自己写：应用要能离线运行、且二维码要能随时按新链接重新生成，
所以不引入 qrcode/Pillow 之外的三方依赖（Pillow 仅用于导出 PNG）。
算法依据 ISO/IEC 18004:2015（QR Code 规范）。
"""
from PIL import Image

# ---------------- GF(256) 伽罗华域 ----------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_gen_poly(n):
    poly = [1]
    for i in range(n):
        new = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            new[j] ^= c
            new[j + 1] ^= _gf_mul(c, _EXP[i])
        poly = new
    return poly


def _rs_ecc(data, n):
    gen = _rs_gen_poly(n)
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            res[i + j] ^= _gf_mul(g, coef)
    return res[len(data):]


# ---------------- 容量表 ----------------
# (版本, 等级) -> (每块纠错码字数, [((块数, 每块数据码字数), ...)])
_RS_TABLE = {
    (1, 'L'): (7, [(1, 19)]),   (1, 'M'): (10, [(1, 16)]), (1, 'Q'): (13, [(1, 13)]), (1, 'H'): (17, [(1, 9)]),
    (2, 'L'): (10, [(1, 34)]),  (2, 'M'): (16, [(1, 28)]), (2, 'Q'): (22, [(1, 22)]), (2, 'H'): (28, [(1, 16)]),
    (3, 'L'): (15, [(1, 55)]),  (3, 'M'): (26, [(1, 44)]), (3, 'Q'): (18, [(2, 17)]), (3, 'H'): (22, [(2, 13)]),
    (4, 'L'): (20, [(1, 80)]),  (4, 'M'): (18, [(2, 32)]), (4, 'Q'): (26, [(2, 24)]), (4, 'H'): (16, [(4, 9)]),
    (5, 'L'): (26, [(1, 108)]), (5, 'M'): (24, [(2, 43)]), (5, 'Q'): (18, [(2, 15), (2, 16)]), (5, 'H'): (22, [(2, 11), (2, 12)]),
    (6, 'L'): (18, [(2, 68)]),  (6, 'M'): (16, [(4, 27)]), (6, 'Q'): (24, [(4, 19)]), (6, 'H'): (28, [(4, 15)]),
    (7, 'L'): (20, [(2, 78)]),  (7, 'M'): (18, [(4, 31)]), (7, 'Q'): (18, [(2, 14), (4, 15)]), (7, 'H'): (26, [(4, 13), (1, 14)]),
    (8, 'L'): (24, [(2, 97)]),  (8, 'M'): (22, [(2, 38), (2, 39)]), (8, 'Q'): (22, [(4, 18), (2, 19)]), (8, 'H'): (26, [(4, 14), (2, 15)]),
    (9, 'L'): (30, [(2, 116)]), (9, 'M'): (22, [(3, 36), (2, 37)]), (9, 'Q'): (20, [(4, 16), (4, 17)]), (9, 'H'): (24, [(4, 12), (4, 13)]),
    (10, 'L'): (18, [(2, 68), (2, 69)]), (10, 'M'): (26, [(4, 43), (1, 44)]), (10, 'Q'): (24, [(6, 19), (2, 20)]), (10, 'H'): (28, [(6, 15), (2, 16)]),
}

# 格式信息中的纠错等级编码
_EC_BITS = {'L': 0b01, 'M': 0b00, 'Q': 0b11, 'H': 0b10}

_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}


def data_capacity(version, ec):
    """该版本 + 纠错等级可容纳的数据码字数（字节模式最大字节数见 max_bytes）。"""
    _, groups = _RS_TABLE[(version, ec)]
    return sum(n * d for n, d in groups)


def max_bytes(version, ec):
    """字节模式下可容纳的字节数。"""
    cap = data_capacity(version, ec)
    return cap - 3 if version >= 10 else cap - 2   # 4bit 模式 + 字符计数(8/16) + 终止符空间


def _choose_version(nbytes, ec):
    for v in range(1, 11):
        # 字符计数指示符：v1-9 为 8 位，v10+ 为 16 位
        cap_bits = data_capacity(v, ec) * 8
        ccbits = 8 if v <= 9 else 16
        need = 4 + ccbits + nbytes * 8
        if need <= cap_bits:
            return v
    raise ValueError("数据过长，超出本生成器支持的 10 个版本范围（最大约 250 字节）")


# ---------------- 码字构造 ----------------
def _build_codewords(payload, version, ec):
    data = list(payload)
    ecw, groups = _RS_TABLE[(version, ec)]
    total_spec = 4 + (8 if version <= 9 else 16) + 8 * len(data)
    capacity = data_capacity(version, ec)
    total_data = capacity

    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                      # 字节模式
    put(len(data), 8 if version <= 9 else 16)
    for b in data:
        put(b, 8)
    # 终止符
    remain = total_data * 8 - len(bits)
    put(0, min(4, remain))
    # 补齐到字节
    while len(bits) % 8 != 0:
        bits.append(0)
    # 补位字节
    pad = [0xEC, 0x11]
    i = 0
    while len(bits) < total_data * 8:
        put(pad[i % 2], 8)
        i += 1

    data_cws = []
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        data_cws.append(byte)

    # 分块 + 纠错
    blocks, idx = [], 0
    for count, dlen in groups:
        for _ in range(count):
            chunk = data_cws[idx:idx + dlen]
            idx += dlen
            blocks.append((chunk, _rs_ecc(chunk, ecw)))

    # 交织
    result = []
    max_dlen = max(len(c) for c, _ in blocks)
    for i in range(max_dlen):
        for c, _ in blocks:
            if i < len(c):
                result.append(c[i])
    for i in range(ecw):
        for _, e in blocks:
            result.append(e[i])
    return result


# ---------------- 矩阵绘制 ----------------
def _new_matrix(size):
    return [[None] * size for _ in range(size)]


def _place_finder(m, r, c):
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < len(m) and 0 <= cc < len(m)):
                continue
            inside = (0 <= dr <= 6 and 0 <= dc <= 6)
            if not inside:
                m[rr][cc] = 0          # 分隔符
                continue
            edge = dr in (0, 6) or dc in (0, 6)
            core = 2 <= dr <= 4 and 2 <= dc <= 4
            m[rr][cc] = 1 if (edge or core) else 0


def _place_alignment(m, version):
    centers = _ALIGN[version]
    if not centers:
        return
    size = len(m)
    for r in centers:
        for c in centers:
            if m[r][c] is not None:      # 与定位图形重叠则跳过
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    d = max(abs(dr), abs(dc))
                    m[r + dr][c + dc] = 1 if d != 1 else 0


def _reserve_format(m):
    size = len(m)
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = 0
        if m[i][8] is None:
            m[i][8] = 0
    for i in range(8):
        m[8][size - 1 - i] = 0
        m[size - 1 - i][8] = 0
    m[size - 8][8] = 1                      # 固定的暗模块


def _place_timing(m):
    size = len(m)
    for i in range(8, size - 8):
        bit = 1 if i % 2 == 0 else 0
        if m[6][i] is None:
            m[6][i] = bit
        if m[i][6] is None:
            m[i][6] = bit


def _place_version_info(m, version):
    if version < 7:
        return
    size = len(m)
    # 版本信息：6 位版本号经 BCH(18,6) 编码，生成多项式 0x1F25
    rem = _bch_remainder(version, 5, 0x1F25, 12)
    val = (version << 12) | rem
    for i in range(18):
        bit = (val >> i) & 1
        r, c = i // 3, i % 3
        m[size - 11 + c][r] = bit
        m[r][size - 11 + c] = bit


def _place_data(m, codewords):
    size = len(m)
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if m[row][c] is None:
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


_MASK_FUNCS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(m, mask, reserved):
    size = len(m)
    out = [row[:] for row in m]
    f = _MASK_FUNCS[mask]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and f(r, c):
                out[r][c] ^= 1
    return out


def _bch_remainder(value, msb_pos, poly, deg):
    """多项式长除法取余：value 左移 deg 位后对生成多项式 poly 取余。"""
    reg = value << deg
    for i in range(msb_pos + deg, deg - 1, -1):
        if (reg >> i) & 1:
            reg ^= poly << (i - deg)
    return reg & ((1 << deg) - 1)


def _format_bits(ec, mask):
    # 格式信息：5 位（2 位纠错等级 + 3 位掩码）经 BCH(15,5) 编码，再与 0x5412 异或
    val = (_EC_BITS[ec] << 3) | mask
    rem = _bch_remainder(val, 4, 0x537, 10)      # g(x)=x^10+x^8+x^5+x^4+x^2+x+1
    return ((val << 10) | rem) ^ 0x5412


def _place_format(m, ec, mask):
    """按 ISO 18004 图 25 放置 15 位格式信息（两份副本）。
    左上：列 8 上行 0-5、行 7、行 8；行 8 上列 7、列 5-0。
    另一份副本：行 8 右侧 8 格 + 列 8 底部 7 格，末尾为固定暗模块。
    """
    size = len(m)
    bits = _format_bits(ec, mask)

    def b(i):
        return (bits >> i) & 1

    # 副本一（围绕左上定位图形）
    for i in range(6):
        m[i][8] = b(i)              # (行 i, 列 8)
    m[7][8] = b(6)
    m[8][8] = b(7)
    m[8][7] = b(8)                  # (行 8, 列 7)
    for i in range(9, 15):
        m[8][14 - i] = b(i)         # (行 8, 列 5..0)

    # 副本二
    for i in range(8):
        m[8][size - 1 - i] = b(i)   # (行 8, 右侧列)
    for i in range(8, 15):
        m[size - 15 + i][8] = b(i)  # (行 底部, 列 8)

    m[size - 8][8] = 1              # 固定暗模块


def _penalty(m):
    size = len(m)
    score = 0
    for r in range(size):
        run, prev = 0, None
        for c in range(size):
            v = m[r][c]
            if v == prev:
                run += 1
            else:
                if prev is not None and run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(size):
        run, prev = 0, None
        for r in range(size):
            v = m[r][c]
            if v == prev:
                run += 1
            else:
                if prev is not None and run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    for r in range(size - 1):
        for c in range(size - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = list(reversed(pat1))
    for r in range(size):
        row = [m[r][c] for c in range(size)]
        for c in range(size - 10):
            seg = row[c:c + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    for c in range(size):
        col = [m[r][c] for r in range(size)]
        for r in range(size - 10):
            seg = col[r:r + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    dark = sum(sum(1 for v in row if v) for row in m)
    percent = dark * 100.0 / (size * size)
    k = int(abs(percent - 50) / 5)
    score += 10 * k
    return score


def make_matrix(text, ec='M', version=None, mask=None):
    """返回 (matrix, version, mask, ec)。matrix[r][c] 为 0/1。"""
    payload = text.encode('utf-8')
    if version is None:
        version = _choose_version(len(payload), ec)
    if len(payload) > max_bytes(version, ec):
        raise ValueError("数据超出该版本容量")

    codewords = _build_codewords(payload, version, ec)
    size = 17 + 4 * version
    base = _new_matrix(size)

    _place_finder(base, 0, 0)
    _place_finder(base, 0, size - 7)
    _place_finder(base, size - 7, 0)
    _place_alignment(base, version)
    _place_timing(base)
    _reserve_format(base)
    _place_version_info(base, version)

    reserved = [[base[r][c] is not None for c in range(size)] for r in range(size)]
    _place_data(base, codewords)

    if mask is None:
        best, best_score, best_m = None, None, None
        for mk in range(8):
            cand = _apply_mask(base, mk, reserved)
            _place_format(cand, ec, mk)
            s = _penalty(cand)
            if best_score is None or s < best_score:
                best, best_score, best_m = mk, s, cand
        return best_m, version, best, ec

    cand = _apply_mask(base, mask, reserved)
    _place_format(cand, ec, mask)
    return cand, version, mask, ec


def to_png(matrix, path, scale=10, border=4):
    size = len(matrix)
    total = (size + border * 2) * scale
    img = Image.new('1', (total, total), 1)
    px = img.load()
    for r in range(size):
        for c in range(size):
            if matrix[r][c]:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(c + border) * scale + dx, (r + border) * scale + dy] = 0
    img.save(path)
    return path


def to_png_bytes(matrix, scale=10, border=4):
    import io
    size = len(matrix)
    total = (size + border * 2) * scale
    img = Image.new('1', (total, total), 1)
    px = img.load()
    for r in range(size):
        for c in range(size):
            if matrix[r][c]:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(c + border) * scale + dx, (r + border) * scale + dy] = 0
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


if __name__ == "__main__":
    m, v, mask, ec = make_matrix("https://anxin-yongdian.onrender.com")
    print("version=%d mask=%d ec=%s size=%d" % (v, mask, ec, len(m)))
    to_png(m, "_qr_test.png", scale=10, border=4)
    for row in m[:8]:
        print("".join("██" if x else "  " for x in row))
