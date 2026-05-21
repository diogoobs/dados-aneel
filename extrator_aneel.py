#!/usr/bin/env python3
"""
Extrator de Tarifas ANEEL — Dashboard Tarifa Branca (Power BI)
==============================================================
INSTALAÇÃO:
    pip install playwright
    playwright install chromium

USO:
    python3 extrator_aneel.py              # com janela do browser
    python3 extrator_aneel.py --headless   # sem janela
"""

import asyncio, json, argparse, re, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    print("❌  Execute: pip install playwright && playwright install chromium")
    sys.exit(1)

DASHBOARD_URL = (
    "https://app.powerbi.com/view?r=eyJrIjoiMTEzZDgyMzctNGQzZS00MTVkLTg3M2UtO"
    "GMwNjBjMzM2MGVmIiwidCI6IjQwZDZmOWI4LWVjYTctNDZhMi05MmQ0LWVhNGU5YzAxNzBl"
    "MSIsImMiOjR9"
)
DEFAULT_OUTPUT = "tarifas_aneel.json"
LOAD_WAIT_MS   = 20_000
RENDER_WAIT_MS = 3_500
TARIFF_MIN, TARIFF_MAX = 0.30, 2.80


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def fmt(v): return f"{v:.3f}" if v is not None else "—"

def is_axis_scale(v):
    """Valores como 0.5, 1.0, 1.5, 2.0 são escala do gráfico, não tarifas."""
    return abs(v * 2 - round(v * 2)) < 0.005   # múltiplos exatos de 0.5

def is_valid_tariff(v, decimal_places=None):
    """
    Tarifas ANEEL sempre têm 3 casas decimais (ex: 0,733).
    Valores de escala do gráfico têm 1 casa (ex: 0,6 ou 0,8).
    """
    if not (TARIFF_MIN <= v <= TARIFF_MAX):
        return False
    # Se temos informação de casas decimais, usar como filtro primário
    if decimal_places is not None:
        return decimal_places >= 3
    # Fallback: exclui múltiplos de 0.2 (cobre eixos com incremento 0.2 e 0.5)
    return abs(v * 5 - round(v * 5)) > 0.01

def parse_float(s):
    try: return float(str(s).replace(",", "."))
    except: return None

def is_distributor_name(s):
    """Filtra nomes reais de distribuidoras — exclui horas, legendas e lixo."""
    s = s.strip()
    if not s or len(s) < 3 or len(s) > 60: return False
    if re.match(r'^\d{2}[:h]\d{2}', s): return False      # 00:00 ou 00h00
    if re.match(r'^\d+$', s): return False                  # só números
    if re.match(r'^\d+[,\.]\d', s): return False          # decimais pt-BR: 0,5 / 0,50,5 / 1,0
    if 'Tarifa' in s or 'tarifa' in s: return False          # legendas do gráfico
    return True


# ─── EXTRAÇÃO: VALORES DOS CARTÕES ────────────────────────────────────────────
EXTRACT_NUMBERS_JS = """
() => {
    const results = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while ((node = walker.nextNode())) {
        const raw = node.textContent.trim();
        if (!raw) continue;
        const num = parseFloat(raw.replace(",", "."));
        if (isNaN(num)) continue;
        // Count decimal places in original text (pt-BR uses comma as decimal separator)
        const sep = raw.includes(",") ? "," : ".";
        const parts = raw.split(sep);
        const decimalPlaces = parts.length > 1 ? parts[parts.length-1].length : 0;
        const el = node.parentElement;
        const rect = el?.getBoundingClientRect?.() ?? {};
        results.push({
            raw, value: num, decimalPlaces,
            tag: el?.tagName ?? "",
            x: rect.left ?? 0, y: rect.top ?? 0,
            w: rect.width ?? 0, h: rect.height ?? 0,
        });
    }
    return results;
}
"""

async def extrair_valores_cartoes(page) -> dict:
    try:
        items = await page.evaluate(EXTRACT_NUMBERS_JS)
    except Exception as e:
        print(f"    ⚠ JS error: {e}"); return {}

    # Filtra candidatos: usa casas decimais como filtro principal
    # Tarifas ANEEL têm 3 casas decimais; escala do gráfico tem 1 ou 2
    candidates = [
        i for i in items
        if is_valid_tariff(i["value"], i.get("decimalPlaces"))
        and i.get("w", 0) > 5
        and i.get("h", 0) > 5
        and 50 < i.get("y", 0) < 800
    ]

    if not candidates:
        # Fallback sem filtro de decimais
        candidates = [
            i for i in items
            if TARIFF_MIN <= i["value"] <= TARIFF_MAX
            and i.get("w", 0) > 5 and i.get("h", 0) > 5
            and 50 < i.get("y", 0) < 800
        ]

    if not candidates: return {}

    # Remove duplicatas próximas
    unique = []
    for c in sorted(candidates, key=lambda x: x["x"]):
        if not any(abs(c["value"] - u["value"]) < 0.002 and abs(c["x"] - u["x"]) < 20 for u in unique):
            unique.append(c)

    print(f"    📊 Candidatos ({len(unique)}): {[round(c['value'],3) for c in unique]}")

    if not unique: return {}

    # Ordena por valor: menor = fora ponta, maior = ponta
    by_value = sorted(unique, key=lambda x: x["value"])
    # Ordem ANEEL: FP < Convencional < Intermediário < Ponta
    if len(by_value) >= 4:
        valores = {
            "foraponta":     by_value[0]["value"],
            "convencional":  by_value[1]["value"],
            "intermediario": by_value[2]["value"],
            "ponta":         by_value[-1]["value"],
        }
    elif len(by_value) == 3:
        valores = {
            "foraponta":     by_value[0]["value"],
            "intermediario": by_value[1]["value"],
            "ponta":         by_value[2]["value"],
            "convencional":  None,
        }
    elif len(by_value) == 2:
        valores = {"foraponta": by_value[0]["value"], "ponta": by_value[1]["value"],
                   "intermediario": None, "convencional": None}
    else:
        valores = {"ponta": by_value[0]["value"], "foraponta": None,
                   "intermediario": None, "convencional": None}
    return valores


# ─── EXTRAÇÃO: RESOLUÇÕES ─────────────────────────────────────────────────────
async def extrair_resolucoes(page) -> dict:
    resolucoes = {}
    try:
        text = await page.evaluate("() => document.body.innerText")
        # Padrão: 3477/2025 ou 3.215/2023
        matches = re.findall(r'\b(\d[\d\.]+)\/(\d{4})\b', text)
        nums = [f"{m[0].replace('.','')}/{m[1]}" for m in matches
                if 2000 <= int(m[1]) <= 2030]   # só anos plausíveis
        if len(nums) >= 2:
            resolucoes["postos"]  = nums[0]
            resolucoes["tarifas"] = nums[1]
        elif nums:
            resolucoes["tarifas"] = nums[0]
    except Exception as e:
        print(f"    ⚠ Resoluções: {e}")
    return resolucoes


# ─── SLICER: LISTAR DISTRIBUIDORAS ────────────────────────────────────────────
GET_OPTIONS_JS = """
() => {
    const names = new Set();
    // Tenta vários seletores comuns de slicer no Power BI
    const selectors = [
        '[role="option"]',
        '[role="listitem"]',
        '.slicerText',
        '[class*="slicerItem"]',
        '[class*="row"] span',
    ];
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            const t = el.textContent.trim();
            if (t.length > 2) names.add(t);
        }
    }
    return Array.from(names);
}
"""

SCROLL_LIST_JS = """
() => {
    // Cada item do dropdown tem ~32px — scroll de 3 itens por vez evita pular distribuidoras
    const STEP = 96;
    let scrolled = 0;
    for (const el of document.querySelectorAll('*')) {
        if (el.scrollHeight > el.clientHeight + 20) {
            const style = window.getComputedStyle(el);
            const ov = style.overflow + style.overflowY + style.overflowX;
            if (ov.includes('auto') || ov.includes('scroll')) {
                el.scrollTop += STEP;
                scrolled++;
            }
        }
    }
    return scrolled;
}
"""

async def abrir_slicer(page):
    strategies = [
        lambda: page.click('[role="combobox"]', timeout=3000),
        lambda: page.click('[aria-label*="Distribuidora"]', timeout=3000),
        lambda: page.click('[aria-label*="distribuidora"]', timeout=3000),
        # Clica no primeiro item visível do slicer para ativar o dropdown
        lambda: page.locator('[role="option"]').first.click(timeout=3000),
    ]
    for fn in strategies:
        try:
            await fn()
            await page.wait_for_timeout(1500)
            return True
        except: pass
    return False

async def get_distribuidoras(page) -> list[str]:
    """Coleta todas as distribuidoras rolando o dropdown até o fim."""
    await abrir_slicer(page)
    await page.wait_for_timeout(1000)

    all_names: set[str] = set()
    prev_count = -1

    for scroll_attempt in range(120):  # rola até 120x (96px/passo × 120 = ~3840px de lista)
        raw = await page.evaluate(GET_OPTIONS_JS)
        # Filtra apenas nomes reais de distribuidoras
        valid = {n for n in raw if is_distributor_name(n)}
        all_names.update(valid)

        if len(all_names) == prev_count:
            break   # nada novo — chegou ao fim da lista
        prev_count = len(all_names)

        # Rola a lista para baixo
        scrolled = await page.evaluate(SCROLL_LIST_JS)
        if scrolled == 0:
            break   # nenhum container encontrado
        await page.wait_for_timeout(400)

    return sorted(all_names)


# ─── SLICER: SELECIONAR DISTRIBUIDORA ─────────────────────────────────────────
async def selecionar_distribuidora(page, nome: str) -> bool:
    """
    Usa click nativo do Playwright (dispara eventos React corretamente).
    Rola o dropdown até encontrar o item e clica.
    """
    for passa in range(2):
        await abrir_slicer(page)
        await page.wait_for_timeout(700)

        for scroll_n in range(80):
            # Tenta com click nativo do Playwright (dispara eventos React/JS do Power BI)
            for sel in [
                f'[role="option"]:has-text("{nome}")',
                f'[role="listitem"]:has-text("{nome}")',
                f'li:has-text("{nome}")',
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=400):
                        await loc.scroll_into_view_if_needed(timeout=1000)
                        await loc.click(timeout=2000)
                        await page.wait_for_timeout(RENDER_WAIT_MS)
                        try: await page.wait_for_load_state("networkidle", timeout=5000)
                        except PwTimeout: pass
                        return True
                except: pass

            await page.evaluate(SCROLL_LIST_JS)
            await page.wait_for_timeout(200)

    return False


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless",       action="store_true")
    parser.add_argument("--output",         default=DEFAULT_OUTPUT)
    parser.add_argument("--distribuidoras", nargs="+")
    parser.add_argument("--debug-dir",      default="debug_screenshots")
    args = parser.parse_args()

    out_path   = Path(args.output)
    debug_path = Path(args.debug_dir)
    debug_path.mkdir(parents=True, exist_ok=True)

    print("""
╔═══════════════════════════════════════════════════╗
║  Extrator de Tarifas ANEEL — Tarifa Branca        ║
╚═══════════════════════════════════════════════════╝""")
    print(f"  Saída: {out_path}  |  Headless: {args.headless}\n")

    resultado = {
        "gerado_em":    datetime.now(timezone.utc).isoformat(),
        "fonte":        "Dashboard Tarifa Branca ANEEL",
        "url_fonte":    DASHBOARD_URL,
        "distribuidoras": {}
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx  = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        page = await ctx.new_page()

        print("📡  Carregando dashboard ANEEL...")
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000)
        print(f"⏳  Aguardando Power BI renderizar (~{LOAD_WAIT_MS//1000}s)...")
        await page.wait_for_timeout(LOAD_WAIT_MS)

        for sel in ['[class*="visual"]', 'svg', '.visual-container']:
            try:
                await page.wait_for_selector(sel, timeout=10_000)
                print(f"  ✅ Visual detectado: {sel}"); break
            except PwTimeout: pass

        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(debug_path / "00_inicial.png"))
        print("  📸 00_inicial.png\n")

        # ── Lista de distribuidoras ──────────────────────────────────────────
        if args.distribuidoras:
            distribuidoras = args.distribuidoras
            print(f"📋  Usando lista fornecida: {len(distribuidoras)}")
        else:
            print("🔍  Detectando distribuidoras no slicer...")
            distribuidoras = await get_distribuidoras(page)

            if distribuidoras:
                print(f"  ✅ {len(distribuidoras)} distribuidoras encontradas")
                for d in distribuidoras[:10]:
                    print(f"     • {d}")
                if len(distribuidoras) > 10:
                    print(f"     ... e mais {len(distribuidoras) - 10}")
            else:
                print("  ⚠  Detecção automática falhou — usando lista padrão")
                distribuidoras = [
                    "Enel SP", "Enel CE", "Enel GO", "EDP SP", "Elektro",
                    "CPFL Paulista", "CPFL Piratininga", "Copel",
                    "Cemig-D", "Light", "Celesc-DIS",
                    "Equatorial MA", "Equatorial PA", "Equatorial AL",
                    "Equatorial PI", "Equatorial GO",
                    "Energisa MT", "Energisa MS", "Energisa PB",
                    "Coelba", "Celpe", "Cosern", "Coelce",
                    "Amazonas Energia", "CERON", "Boa Vista Energia",
                ]

        # ── Extrai dados por distribuidora ───────────────────────────────────
        total, ok_count = len(distribuidoras), 0

        for idx, distrib in enumerate(distribuidoras, 1):
            print(f"\n[{idx:02d}/{total}] {distrib}")

            if not await selecionar_distribuidora(page, distrib):
                print(f"  ✗  Não foi possível selecionar")
                continue

            safe = re.sub(r'[^a-zA-Z0-9_]', '_', distrib)
            await page.screenshot(path=str(debug_path / f"{idx:02d}_{safe}.png"))

            valores    = await extrair_valores_cartoes(page)
            resolucoes = await extrair_resolucoes(page)

            fp, int_, p, c = (valores.get(k) for k in
                              ["foraponta","intermediario","ponta","convencional"])

            # Detecção de trava: valores idênticos à distribuidora anterior = seleção não funcionou
            ultimas = list(resultado["distribuidoras"].values())
            if ultimas and fp is not None and p is not None:
                ult = ultimas[-1].get("branca_b", {})
                if fp == ult.get("foraponta") and p == ult.get("ponta"):
                    print(f"  ⚠  TRAVA detectada — valores == distribuidora anterior, pulando")
                    continue

            if fp or int_ or p:
                print(f"  ✓  FP={fmt(fp)} | Int={fmt(int_)} | P={fmt(p)} | Conv={fmt(c)}")
                print(f"     Resol: tarifas={resolucoes.get('tarifas','?')} "
                      f"| postos={resolucoes.get('postos','?')}")

                resultado["distribuidoras"][distrib] = {
                    "sigla":              distrib.upper()[:12],
                    "nome_comercial":     distrib,
                    "atualizado":         datetime.now(timezone.utc).date().isoformat(),
                    "resolucao_tarifas":  resolucoes.get("tarifas"),
                    "resolucao_postos":   resolucoes.get("postos"),
                    "convencional_b":     {"tarifa": c},
                    "branca_b":           {"foraponta": fp, "intermediario": int_, "ponta": p},
                    "horarios_branca":    {
                        "ponta": None, "intermediario": None, "foraponta": None,
                        "fonte": "Dashboard Tarifa Branca ANEEL",
                        "nota":  "Horários visíveis nos screenshots de debug",
                    },
                }
                ok_count += 1
            else:
                print(f"  ✗  Sem valores — veja {idx:02d}_{safe}.png")

        await browser.close()

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    print(f"""
╔═══════════════════════════════════════════════════╗
║  Concluído! {ok_count}/{total} distribuidoras      
║  → {out_path}
╚═══════════════════════════════════════════════════╝

Próximos passos:
  git add tarifas_aneel.json
  git commit -m "chore: atualiza tarifas ANEEL $(date -u +%Y-%m-%d)"
  git push
""")

if __name__ == "__main__":
    asyncio.run(main())
