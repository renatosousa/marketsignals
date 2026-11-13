/* Comum do painel novo (/v2): navegacao, formatos, grafico uPlot, barra de correlacao e laco de atualizacao. */
(() => {
  "use strict";

  const graficos = Object.create(null);
  const errosLaco = new Map();

  const $ = s => document.querySelector(s);
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const cores = () => [css("--s1"), css("--s2"), css("--s3"), css("--s4")];

  const nf = d => new Intl.NumberFormat("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const numero = (v, d) => {
    const n = Number(v);
    const p = 10 ** d;
    return Math.round(n * p) === 0 ? 0 : n;
  };
  const menos = s => s.replace("-", "−");  // sinal de menos tipografico em todos os numeros
  const fmt = (v, d = 2) => v == null || v === "" || Number.isNaN(Number(v)) ? "–" : menos(nf(d).format(numero(v, d)));
  const sinal = (v, d = 2) => v == null || v === "" || Number.isNaN(Number(v)) ? "–"
    : (numero(v, d) > 0 ? "+" : "") + menos(nf(d).format(numero(v, d)));
  const hora = ts => {
    if (ts == null || ts === "") return "–";
    const t = new Date(Number(ts) * 1000);
    return Number.isNaN(t.getTime()) ? "–" : t.toISOString().slice(11, 16);
  };
  const corr = v => {
    const s = sinal(v, 2);
    return s === "–" ? s : s.replace("-", "−");
  };
  const dataBR = iso => {
    if (!iso || iso.length < 10) return "–";
    return `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}`;
  };
  const dataBRCurta = iso => dataBR(iso).slice(0, 5);
  const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const classeSinal = v => v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
  const janelaNome = j => ({ "3600": "1 h", "10800": "3 h", "pregao": "Pregão" }[j] || j || "–");
  const ativoCurto = s => String(s || "").replace(/^(WIN|WDO|IND)[FGHJKMNQUVXZ]\d{2}$/, "$1");

  const NOMES = {
    "WIN$": "WIN", "WDO$": "Dólar", "IND$": "Índice", "BIT$": "Bitcoin",
    "DI1F27": "DI jan/27", "DI1F28": "DI jan/28", "DI1F29": "DI jan/29", "DI1F33": "DI jan/33",
    "SPX_USD": "S&P 500 em dólar", "NDX_USD": "Nasdaq em dólar", "BEWZ39": "EWZ (BDR)",
    "VALE3": "Vale", "PETR4": "Petrobras", "PRIO3": "PRIO", "ITUB4": "Itaú",
    "BBAS3": "Banco do Brasil", "SMAL11": "Small caps", "GOLD11": "Ouro",
    "IVVB11": "IVVB11", "NASD11": "NASD11",
  };
  const nome = s => NOMES[s] || s || "–";
  const parNome = (y, x) => `${nome(y)} × ${nome(x)}`;
  const ehTaxa = s => String(s || "").startsWith("DI1") || String(s || "").startsWith("DAP");

  // ---------- navegacao comum
  const LINKS = [
    ["Resumo", "/v2"], ["Correlações", "/v2/correlacoes"], ["Fluxo", "/v2/fluxo"],
    ["Painel atual", "/"], ["Gráfico WIN", "/grafico?ativo=WIN"], ["Gráfico WDO", "/grafico?ativo=WDO"],
    ["Players", "/players"], ["Leilões", "/leiloes"],
  ];
  function nav(atual) {
    const main = document.querySelector("main");
    if (!main) return;
    const el = document.createElement("nav");
    el.className = "paginas";
    el.setAttribute("aria-label", "Páginas");
    el.innerHTML = LINKS.map(([txt, href]) =>
      `<a href="${href}"${href === atual ? ' aria-current="page"' : ""}>${txt}</a>`).join("");
    main.prepend(el);
  }

  // ---------- barra de correlacao (-1 a +1)
  function barraCorr(v) {
    if (v == null) return '<span class="cbar" aria-hidden="true"><span></span><span></span></span>';
    const w = Math.min(Math.abs(Number(v)), 1) * 60;
    return Number(v) < 0
      ? `<span class="cbar" aria-hidden="true"><span><i class="barra neg" style="width:${w}px;background:var(--neg)"></i></span><span></span></span>`
      : `<span class="cbar" aria-hidden="true"><span></span><span><i class="barra" style="width:${w}px;background:var(--pos)"></i></span></span>`;
  }

  // ---------- regua -1..+1 com a faixa p10-p90 e o valor de hoje
  function regua(p10, p90, atual) {
    if (p10 == null || p90 == null) return '<span class="nota">sem normal</span>';
    const pos = v => Math.max(0, Math.min(100, (Number(v) + 1) / 2 * 100));
    let a = pos(p10), b = pos(p90);
    if (b < a) { const t = a; a = b; b = t; }
    const larg = Math.max(2, b - a);
    const agora = atual == null ? "" : `<span class="regua-agora" style="left:${pos(atual)}%"></span>`;
    return `<span class="regua" title="p10 ${corr(p10)} · p90 ${corr(p90)}">` +
      `<span class="regua-faixa" style="left:${a}%;width:${larg}%"></span>${agora}</span>`;
  }

  // ---------- grafico generico (uPlot) com crosshair, legenda viva e tabela gemea
  function linhaZero(u) {
    const y = u.valToPos(0, "y", true);
    if (!Number.isFinite(y) || y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;
    const c = u.ctx;
    c.save(); c.strokeStyle = css("--axis"); c.lineWidth = 1;
    c.beginPath(); c.moveTo(u.bbox.left, y); c.lineTo(u.bbox.left + u.bbox.width, y); c.stroke(); c.restore();
  }

  function desenhaFaixa(u, faixa) {
    if (faixa == null || faixa.de == null || faixa.ate == null) return;
    const escala = faixa.eixo === 2 ? "y2" : "y";
    let a = u.valToPos(faixa.de, escala, true), b = u.valToPos(faixa.ate, escala, true);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return;
    if (b < a) { const t = a; a = b; b = t; }
    const alt = Math.max(0, b - a);
    if (!alt) return;
    const c = u.ctx;
    c.save(); c.fillStyle = css("--wash");
    c.fillRect(u.bbox.left, a, u.bbox.width, alt);
    c.restore();
  }

  function assinaturaDe(series, o) {
    return JSON.stringify([
      series.map(s => s.nome),
      o.tempo !== false,
      o.xCat || null,
      o.eixo2 == null ? null : o.eixo2,
      o.faixa && o.faixa.de != null ? o.faixa.de : null,
      o.faixa && o.faixa.ate != null ? o.faixa.ate : null,
      !!o.soPontos, !!o.pontos,
    ]);
  }

  function grafico(id, x, series, o = {}) {
    const el = document.getElementById(id);
    if (!el) return;
    series = series || [];
    const temDado = series.some(s => s.v && s.v.some(v => v != null));
    const assinatura = assinaturaDe(series, o);
    if (!temDado) {
      if (graficos[id]) { graficos[id].u.destroy(); delete graficos[id]; }
      el.innerHTML = '<div class="vazio">sem dados na janela</div>';
      const det = el.parentElement && el.parentElement.querySelector("details.tabela-grafico");
      if (det) det.querySelector(".tab-wrap").innerHTML = "";
      return;
    }
    const dadosU = [x, ...series.map(s => s.v)];
    const g = graficos[id];
    if (g && g.assinatura === assinatura) {
      g.x = x; g.series = series; g.o = o;
      g.u.setData(dadosU);
    } else {
      if (g) g.u.destroy();
      el.innerHTML = "";
      const cs = cores();
      const d = o.dec == null ? 2 : o.dec;
      const temEixo2 = o.eixo2 != null && o.eixo2 >= 0 && o.eixo2 < series.length;
      const opts = {
        width: el.clientWidth || 400,
        height: o.altura || 220,
        tzDate: ts => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
        scales: {
          x: { time: o.tempo !== false },
          y: { auto: true },
          ...(temEixo2 ? { y2: { auto: true } } : {}),
        },
        cursor: o.tempo === false ? { points: { size: 8 } } : { sync: { key: "t" }, points: { size: 8 } },
        legend: { live: true },
        series: [
          { value: (u, v) => v == null ? "–" : (o.tempo === false ? (o.xCat ? o.xCat[v] : fmt(v, o.decX == null ? 2 : o.decX)) : hora(v)) },
          ...series.map((s, i) => {
            const slot = s.slot == null ? i : s.slot;
            const cfg = {
              label: s.nome,
              stroke: cs[slot % cs.length],
              width: 2,
              spanGaps: true,
              points: {
                show: o.pontos || false, size: 8,
                fill: cs[slot % cs.length], stroke: css("--surface"), width: 2,
              },
              value: (u, v) => v == null ? "–" : (s.fmtY ? s.fmtY(v) : o.fmtY ? o.fmtY(v) : fmt(v, d)),
            };
            if (temEixo2 && i === o.eixo2) cfg.scale = "y2";
            if (o.soPontos) cfg.paths = () => null;
            return cfg;
          }),
        ],
        axes: [
          {
            stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 },
            ticks: { stroke: css("--grid"), width: 1 },
            ...(o.xCat
              ? {
                  splits: () => o.xCat.map((_, i) => i).filter(i => i % (o.xPasso || 1) === 0),
                  values: (u, sp) => sp.map(i => o.xCat[i] == null ? "" : o.xCat[i]),
                }
              : o.tempo === false
                ? { values: (u, sp) => sp.map(v => fmt(v, o.decX == null ? 0 : o.decX)) }
                : { values: (u, sp) => sp.map(v => hora(v).slice(0, 5)) }),
          },
          {
            scale: "y", stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 },
            ticks: { stroke: css("--grid"), width: 1 }, size: 64,
            values: (u, sp) => sp.map(v => o.fmtEixo ? o.fmtEixo(v) : fmt(v, o.decEixo == null ? d : o.decEixo)),
          },
          ...(temEixo2 ? [{
            scale: "y2", side: 1, stroke: css("--muted"), grid: { show: false },
            ticks: { stroke: css("--grid"), width: 1 }, size: 64,
            values: (u, sp) => sp.map(v => o.fmtEixo2 ? o.fmtEixo2(v) : fmt(v, o.decEixo2 == null ? d : o.decEixo2)),
          }] : []),
        ],
        hooks: {},
      };
      const draw = [];
      if (o.zero) draw.push(linhaZero);
      if (o.faixa && o.faixa.de != null && o.faixa.ate != null) draw.push(u => desenhaFaixa(u, o.faixa));
      if (draw.length) opts.hooks.draw = draw;
      graficos[id] = { u: new uPlot(opts, dadosU, el), assinatura, x, series, o };
    }
    tabela(el, x, series, o);
  }

  // tabela gemea (ate 20 linhas espacadas) para leitura sem hover
  function tabela(el, x, series, o) {
    let det = el.parentElement.querySelector("details.tabela-grafico");
    if (!det) {
      det = document.createElement("details");
      det.className = "tabela-grafico";
      det.innerHTML = "<summary>Tabela</summary><div class='tab-wrap'></div>";
      el.after(det);
    }
    const idx = [];
    const passo = Math.max(1, Math.floor(x.length / 20));
    for (let i = x.length - 1; i >= 0 && idx.length < 20; i -= passo) {
      if (series.some(s => s.v[i] != null)) idx.push(i);
    }
    const fx = i => o.tempo === false ? (o.xCat ? o.xCat[x[i]] : fmt(x[i], o.decX == null ? 2 : o.decX)) : hora(x[i]);
    const fy = (s, v) => v == null ? "–" : (s.fmtY ? s.fmtY(v) : o.fmtY ? o.fmtY(v) : fmt(v, o.dec == null ? 2 : o.dec));
    det.querySelector(".tab-wrap").innerHTML =
      `<table><thead><tr><th>${o.tempo === false ? (o.nomeX || "x") : "hora"}</th>` +
      series.map(s => `<th>${esc(s.nome)}</th>`).join("") + "</tr></thead><tbody>" +
      idx.map(i => `<tr><td>${fx(i)}</td>${series.map(s => `<td>${fy(s, s.v[i])}</td>`).join("")}</tr>`).join("") +
      "</tbody></table>";
  }

  function redesenhar() {
    const snaps = Object.entries(graficos).map(([id, g]) => [id, g.x, g.series, g.o]);
    Object.values(graficos).forEach(g => g.u.destroy());
    Object.keys(graficos).forEach(k => delete graficos[k]);
    snaps.forEach(([id, x, series, o]) => grafico(id, x, series, o));
  }

  if (typeof ResizeObserver !== "undefined" && document.body) {
    new ResizeObserver(() => {
      Object.entries(graficos).forEach(([id, g]) => {
        const w = document.getElementById(id) && document.getElementById(id).clientWidth;
        if (w && w !== g.u.width) g.u.setSize({ width: w, height: g.u.height });
      });
    }).observe(document.body);
  }
  if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redesenhar);
  }

  // ---------- decomposicao do WIN (componente usado no Resumo e em Correlacoes)
  function decomposicao(el, d) {
    if (!el) return;
    if (!d) {
      el.innerHTML = '<div class="vazio">sem decomposição na janela</div>';
      return;
    }
    if (d.motivo) {
      el.innerHTML = `<div class="vazio">${esc(d.motivo)}</div>`;
      return;
    }
    const linhas = (d.fatores || []).map(f => ({ nome: f.nome, pts: f.pts, pct: f.pct, proprio: false }));
    linhas.push({ nome: "Próprio", pts: d.propria_pts, pct: d.propria_pct, proprio: true });
    const max = Math.max(1, ...linhas.map(l => Math.abs(Number(l.pts) || 0)));
    const rows = linhas.map(l => {
      const pts = Number(l.pts) || 0;
      const larg = Math.abs(pts) / max * 50;
      const cls = pts < 0 ? "neg" : "pos";
      return `<div class="decomp-linha">
        <span class="decomp-rot" title="${esc(l.nome)}">${esc(l.nome)}</span>
        <span class="decomp-trilho"><span class="decomp-barra ${cls}" style="width:${larg}%"></span></span>
        <span class="decomp-val ${classeSinal(pts)}" title="${sinal(l.pct, 2)}%">${sinal(pts, 0)} pts</span>
      </div>`;
    }).join("");
    const janela = janelaNome(d.janela);
    const n = d.n == null ? "" : ` · n = ${fmt(d.n, 0)}`;
    const betas = d.betas_data ? ` · betas de ${dataBR(d.betas_data)}` : "";
    el.innerHTML =
      `<div class="decomp-top">
        <span class="decomp-total">WIN ${sinal(d.total_pts, 0)} pts <span class="unid">(${sinal(d.total_pct, 2)}%)</span></span>
        <span class="nota" style="margin:0">janela ${esc(janela)}${n}${betas}</span>
      </div>
      <div class="decomp-lista">${rows}</div>
      <div class="decomp-texto">${esc(d.texto || "")}</div>`;
  }

  // ---------- laco de atualizacao, pausando quando a aba esta oculta
  function renderErros() {
    const el = document.getElementById("erro");
    if (el) el.textContent = Array.from(errosLaco.values()).join(" · ");
  }

  function laco(fn, ms) {
    const chave = `laco-${Math.random().toString(36).slice(2)}`;
    let rodando = false, timer = null;
    async function passo() {
      if (document.hidden || rodando) return;
      rodando = true;
      try {
        await fn();
        errosLaco.delete(chave);
      } catch (e) {
        errosLaco.set(chave, `Erro ao atualizar: ${e && e.message ? e.message : e}`);
      } finally {
        rodando = false;
        renderErros();
      }
    }
    passo();
    timer = setInterval(passo, ms);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) passo(); });
    return { parar: () => clearInterval(timer), agora: passo };
  }

  window.V2 = {
    nav, fmt, sinal, hora, corr, dataBR, dataBRCurta, esc, classeSinal, janelaNome, ativoCurto,
    nome, parNome, ehTaxa, barraCorr, regua, grafico, decomposicao, laco, redesenhar,
  };
})();
