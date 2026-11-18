/* Cockpit v3 (/v3): atualiza /api/v3/cockpit a cada 5 s, pausa com a aba oculta e desenha as
   tres colunas (cenario, regua tatica e playbooks). Usa os formatos e o laco do comum.js (V2). */
(() => {
  "use strict";

  const $ = s => document.querySelector(s);
  const esc = V2.esc;
  const fmt = V2.fmt;
  const sinal = V2.sinal;
  const num = v => (v == null || v === "" || Number.isNaN(Number(v)) ? null : Number(v));

  const ORDEM_PILARES = ["macro_juros", "fluxo_cambio", "vol_opcoes", "global_commod", "noticias"];
  const STATUS = {
    DISTANTE: ["gat-distante", "aguardando toque"],
    PROXIMA: ["gat-proxima", "próxima do toque"],
    EM_TESTE: ["gat-emteste", "em teste (sem defesa ainda)"],
    GATILHO_CONFIRMADO: ["gat-confirmado", "gatilho confirmado"],
  };
  const ROTULO_ALINHAMENTO = {
    A_FAVOR_TENDENCIA: "A FAVOR DA TENDÊNCIA",
    CONTRA_TENDENCIA: "CONTRA A TENDÊNCIA",
    ALVO: "ALVO",
    NEUTRA: "NEUTRA",
  };
  const CASAS = { "WIN$": 0, "WDO$": 1, "BOVA11": 2 };
  const NOME_ATIVO = { "WIN$": "WIN$", "WDO$": "WDO$", "BOVA11": "BOVA11" };

  let estado = null;
  let recebidoEm = 0;
  let ativoSel = "WIN$";
  let detalheZona = null;
  let abaDir = "opcoes";

  const erroDe = x => (x && x.erro
    ? `<div class="erro-peca">${esc(x.erro)}${x.idade_s != null ? ` · último valor bom há ${fmt(x.idade_s, 0)} s` : ""}</div>`
    : "");

  const agoraConvencao = () => {
    if (!estado || !estado.ts_ms) return Date.now();
    return Number(estado.ts_ms) + (Date.now() - recebidoEm);
  };

  const classeScore = v => (v == null ? "neutro" : v > 0 ? "pos" : v < 0 ? "neg" : "neutro");
  const clsCor = v => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

  function quando(ts) {
    const d = Number(ts) - agoraConvencao();
    const abs = Math.abs(d);
    let txt;
    if (abs < 60e3) txt = `${Math.max(0, Math.round(abs / 1000))} s`;
    else if (abs < 3600e3) txt = `${Math.round(abs / 60e3)} min`;
    else if (abs < 86400e3) txt = `${Math.round(abs / 3600e3)} h`;
    else txt = `${Math.round(abs / 86400e3)} d`;
    return d >= 0 ? `em ${txt}` : `há ${txt}`;
  }

  function hhmm(ts) {
    if (ts == null) return "–";
    const d = new Date(Number(ts));
    return Number.isNaN(d.getTime()) ? "–" : d.toISOString().slice(11, 16);
  }

  // ---------------------------------------------------------------- cabecalho
  function badge(rot, valor, extra, cls) {
    return `<span class="badge ${cls || ""}"><span class="rot">${esc(rot)}</span>` +
      `<span class="val">${valor}</span>${extra ? `<span class="rot">${esc(extra)}</span>` : ""}</span>`;
  }

  function validacaoTexto(v) {
    if (!v || !v.n) return "Score sem validação medida hoje.";
    const acerto = v.acerto_35_dia == null ? "–" : `${fmt(v.acerto_35_dia * 100, 0)}%`;
    const seguintes = (v.postos_seguinte != null || v.postos_5p != null)
      ? `; ${sinal(v.postos_seguinte, 2)} no dia seguinte e ${sinal(v.postos_5p, 2)} em 5 pregões` : "";
    if (v.invertido) {
      const f = (v.fora || {}).dia || {};
      const fora = f.n >= 4 && f.postos != null
        ? `fora da amostra (${fmt(f.n, 0)} pregões): ${sinal(f.postos, 2)}` +
          (f.acerto != null ? `, acerto de ${fmt(f.acerto * 100, 0)}% (${fmt(f.n_sinal, 0)} sinais)` : "")
        : `fora da amostra: ${fmt(f.n || 0, 0)} pregão(ões), ainda sem medida`;
      return `Score invertido desde ${String(v.invertido_desde || "").split("-").reverse().join("/")} ` +
        `(com os pesos da espec a correlação era negativa). Na amostra usada para decidir: ` +
        `${sinal(v.postos_dia, 2)} com o retorno do dia em ${fmt(v.n, 0)} pregões${seguintes}; ` +
        `acerto de ${acerto} com |score| ≥ 35. ${fora[0].toUpperCase()}${fora.slice(1)}. ` +
        `Hipótese em teste.`;
    }
    return `Score com os pesos da espec: correlação ${sinal(v.postos_dia, 2)} com o retorno do dia em ` +
      `${fmt(v.n, 0)} pregões${seguintes}; acerto de ${acerto} com |score| ≥ 35` +
      `${v.n_sinal_dia ? ` (${fmt(v.n_sinal_dia, 0)} sinais)` : ""}. ` +
      `Leia como contexto, não como direção.`;
  }

  function resumoMobile(e) {
    const ce = e.cenario || {};
    const rot = (ce.rotulos || {});
    const parte = (e.regioes || {})[ativoSel] || {};
    const zonas = parte.zonas || [];
    let perto = null;
    for (const z of zonas) {
      const d = Math.abs(num(z.distancia_pts) || 0);
      if (!perto || d < Math.abs(num(perto.distancia_pts) || 0)) perto = z;
    }
    const zonaTxt = perto
      ? `${perto.lado === "ACIMA" ? "↑" : "↓"} ${fmt(perto.preco_centro, CASAS[ativoSel])} ` +
        `(${sinal(perto.distancia_pts, CASAS[ativoSel])} pts · força ${fmt(perto.forca, 0)})`
      : "sem zona próxima";
    return `<span>Viés <b class="${clsCor(ce.score_estrutural)}">${sinal(ce.score_estrutural, 0)} ${esc(rot.estrutural || "")}</b></span>` +
      `<span>Zona mais próxima <b>${zonaTxt}</b></span>`;
  }

  function cabecalho(e) {
    const s = e.saude || {};
    const cls = (s.erro || e.vazio) ? "critico" : (num(s.problemas) > 0 ? "atencao" : "ok");
    $("#saude").className = `saude ${cls}`;
    $("#saude").title = ((s.itens || []).filter(i => i.status !== "OK")
      .map(i => `${i.coletor}: ${i.status}${i.detalhe ? ` (${i.detalhe})` : ""}`).join("\n") || "todas as fontes em dia");
    $("#saude-txt").textContent = s.resumo || (s.erro ? "saúde indisponível"
      : (e.vazio ? "sem estado do cockpit" : "carregando…"));

    const ev = e.alerta_evento || e.proximo_evento;
    const evEl = $("#evento");
    if (ev) {
      evEl.className = `evento${e.alerta_evento ? " alerta" : ""}`;
      evEl.textContent = `${e.alerta_evento ? "AGORA · " : ""}${ev.pais || ""} ${ev.evento || ""} · ${quando(ev.ts_ms)}`;
      evEl.title = `${hhmm(ev.ts_ms)} · ${ev.evento || ""} (${ev.impacto || "alto"})`;
    } else {
      evEl.className = "evento";
      evEl.textContent = "sem evento alto à vista";
    }
    $("#atualizado").textContent = e.idade_s == null ? "sem estado"
      : `últ. atualiz. ${fmt(e.idade_s, 0)} s${e.ts_ms ? ` · ${hhmm(e.ts_ms)}` : ""}`;

    const ce = e.cenario || {};
    const rot = ce.rotulos || {};
    const gex = ce.gex || {};
    const arq = ce.arquetipo || {};
    $("#badges").innerHTML =
      badge("Viés estrutural", `${sinal(ce.score_estrutural, 0)} <span class="rot">${esc(rot.estrutural || "")}</span>`, null,
        classeScore(ce.score_estrutural)) +
      badge("Clima intraday", `${sinal(ce.score_intraday, 0)} <span class="rot">${esc(rot.intraday || "")}</span>`, null,
        classeScore(ce.score_intraday)) +
      badge("Arquétipo", esc(arq.nome || "–"), null, "neutro") +
      badge("GEX", esc(gex.rotulo || "SEM GEX"), gex.valor == null ? null : `${sinal(gex.valor / 1e6, 0)} mi`,
        gex.valor == null ? "neutro" : gex.valor < 0 ? "gex-neg" : "pos");

    $("#validacao").innerHTML = erroDe(ce) + `<b>${esc(validacaoTexto(ce.validacao))}</b>`;
    const vm = $("#validacao-mobile");
    if (vm) vm.innerHTML = `<b>${esc(validacaoTexto(ce.validacao))}</b>`;

    const aviso = $("#aviso");
    if (e.desatualizado) {
      aviso.hidden = false;
      aviso.textContent = e.vazio
        ? "Estado do cockpit vazio: o atualizador_cockpit.py ainda não gravou (ou o banco é outro)."
        : `Estado desatualizado${e.motivo_idade ? ` — ${e.motivo_idade}` : ""}. Os números podem estar velhos.`;
    } else {
      aviso.hidden = true;
    }
    $("#resumo-mobile").innerHTML = resumoMobile(e);
  }

  // ---------------------------------------------------------------- esquerda
  function briefing(e) {
    const ce = e.cenario || {};
    const linhas = ce.briefing || [];
    $("#briefing").innerHTML = linhas.length
      ? linhas.map(l => `<li>${esc(l)}</li>`).join("")
      : `<li class="nota">sem briefing: ${esc((ce.erro || "o motor de cenário ainda não calculou") )}</li>`;
  }

  function pilares(e) {
    const ce = e.cenario || {};
    const p = ce.pilares || {};
    const html = ORDEM_PILARES.map(chave => {
      const x = p[chave];
      if (!x) return "";
      const s = num(x.score);
      const larg = s == null ? 0 : Math.min(50, Math.abs(s) / 2);
      const cls = s == null ? "" : s >= 0 ? "pos" : "neg";
      const tat = (x.tatico || {}).score;
      const inds = (x.indicadores || []).map(i =>
        `<tr><td>${esc(i.nome)}</td><td>${fmt(i.valor, Math.abs(num(i.valor) || 0) >= 1000 ? 0 : 4)}</td>` +
        `<td>${esc(i.unidade || "")}</td><td>${sinal(i.z, 2)}</td></tr>`).join("");
      const faltou = (x.faltou || []).length
        ? `<div class="pilar-obs">faltou: ${esc((x.faltou || []).join(" · "))}</div>` : "";
      return `<div class="pilar" title="${esc(x.nome)} · peso ${fmt(x.peso * 100, 0)}%">
        <div class="pilar-linha"><span class="nome">${esc(x.nome)}</span>` +
        `<span class="num ${clsCor(s)}">${sinal(s, 0)}</span></div>` +
        `<div class="pilar-trilho"><span class="pilar-barra ${cls}" style="width:${larg}%"></span></div>` +
        `<div class="pilar-obs">peso ${fmt(x.peso * 100, 0)}% · z ${sinal(x.z, 2)} · ` +
        `intradiário ${sinal(tat, 0)} · ${(x.indicadores || []).length} indicadores</div>` +
        (inds ? `<details><summary>origem e qualidade</summary><div class="tab-wrap"><table>` +
          `<thead><tr><th>Indicador</th><th>Valor</th><th>Unid.</th><th>z</th></tr></thead><tbody>${inds}</tbody></table></div>` +
          `${faltou}</details>` : faltou) +
        `</div>`;
    }).join("");
    $("#pilares").innerHTML = html || `<div class="vazio">sem pilares calculados</div>`;
  }

  function aberturaItem(nome, a, casas) {
    if (!a) return "";
    if (a.erro) return `<div class="dupla"><span class="rot">${nome}</span><span class="det">${erroDe(a)}</span></div>`;
    const prev = num(a.abertura_prevista);
    const real = num(a.abertura_real);
    const gap = num(a.gap_previsto_pct);
    const sigma = num(a.sigma_erro);
    const desvio = num(a.desvio_sigma);
    const r2 = num(a.r2);
    return `<div class="dupla"><span class="rot">${nome}</span><span class="det">` +
      `prevista <b class="num-g">${fmt(prev, casas)}</b>${gap == null ? "" : ` (gap ${sinal(gap, 2)}%)`}` +
      ` × real <b class="num-g">${real == null ? "–" : fmt(real, casas)}</b>` +
      `<br><span class="nota">sigma ${sigma == null ? "–" : fmt(sigma, casas)} pts · ` +
      `desvio ${desvio == null ? "–" : sinal(desvio, 2) + "σ"} · R² ${r2 == null ? "–" : fmt(r2, 2)}` +
      `${a.n_metricas ? ` (${fmt(a.n_metricas, 0)} pregões)` : ""}</span>` +
      (a.distorcao ? `<br><span class="aviso-lento">Distorção de abertura vs pares globais (&gt; 1,5σ)</span>` : "") +
      `</span></div>`;
  }

  function abertura(e) {
    const a = e.abertura || {};
    $("#abertura").innerHTML = a.erro ? erroDe(a)
      : aberturaItem("WIN$", a["WIN$"], 0) + aberturaItem("WDO$", a["WDO$"], 1) ||
        `<div class="vazio">sem previsão de abertura</div>`;
  }

  function risco(e) {
    const r = e.risco_brasil || {};
    if (r.erro) { $("#risco").innerHTML = erroDe(r); return; }
    const eps = num(r.eps_acum_pct);
    const incl = num(r.incl_var_bps);
    $("#risco").innerHTML =
      `<div class="duplas">
        <div class="dupla"><span class="rot">eps_BR</span><span class="det"><b class="${clsCor(eps)}">${sinal(eps, 2)}%</b>
          no dia · inclinação DI 33-28 <b>${sinal(incl, 0)} bps</b></span></div>
        <div class="dupla"><span class="rot">Leitura</span><span class="det">${esc(r.rotulo || "–")}</span></div>
        <div class="dupla"><span class="rot">Modelo</span><span class="det">WDO ~ DXY + cesta EM, ${esc(r.modelo || "")};
          R² ${fmt(r.r2, 2)} · ${fmt(r.n_janelas, 0)} janelas de 5 min</span></div>
      </div>`;
  }

  function globalBloco(e) {
    const g = e.global || {};
    const ats = (g.ativos || []).filter(a => num(a.variacao_pct) != null);
    if (g.erro || !ats.length) {
      $("#global").innerHTML = erroDe(g) || `<div class="vazio">sem cotação global desde as 18:00 de D-1</div>`;
      return;
    }
    $("#global").innerHTML = `<div class="global-lista">` + (g.ativos || []).map(a => {
      const v = num(a.variacao_pct);
      return `<div class="gl" title="${esc(a.ativo)} · último ${a.ultimo == null ? "–" : fmt(a.ultimo, 2)}` +
        `${a.ts_ms ? " · " + hhmm(a.ts_ms) : ""}"><span>${esc(a.nome)}</span>` +
        `<span class="${clsCor(v)}">${v == null ? "–" : sinal(v, 2) + "%"}</span></div>`;
    }).join("") + `</div><div class="fonte">${esc(g.referencia || "")}</div>`;
  }

  // ---------------------------------------------------------------- regua
  function zonaHtml(z) {
    const casas = CASAS[ativoSel];
    const st = (z.gatilho || {}).status || "DISTANTE";
    const [cls, txt] = STATUS[st] || ["gat-distante", st];
    const alin = ROTULO_ALINHAMENTO[z.alinhamento] || z.alinhamento || "–";
    const selecionada = detalheZona === z.id_zona ? " selecionada" : "";
    return `<button type="button" class="zona ${cls}${selecionada}" data-id="${esc(z.id_zona)}" ` +
      `aria-label="Zona ${esc(z.id_zona)} em ${fmt(z.preco_centro, casas)}">` +
      `<span class="zona-top"><b>${esc(z.direcao)} ${esc(alin)}</b><span class="forca">força ${fmt(z.forca, 0)}</span></span>` +
      `<span class="zona-preco">${fmt(z.preco_centro, casas)}<span class="dist">${sinal(z.distancia_pts, casas)} pts ` +
      `(${sinal(z.distancia_pct, 2)}%)</span></span>` +
      `<span class="zona-comp">${esc((z.componentes || []).join(" · "))}</span>` +
      `<span class="gat">${esc(txt)}</span></button>`;
  }

  function linhaPreco(parte) {
    const casas = CASAS[ativoSel];
    const preco = num(parte.preco);
    const er = num(parte.er);
    return `<div class="linha-preco">
      <span class="preco">${preco == null ? "–" : fmt(preco, casas)}</span>
      <span class="met">VWAP <b>${parte.vwap == null ? "–" : fmt(parte.vwap, casas)}</b></span>
      <span class="met">Gamma flip <b>${parte.gamma_flip == null ? "–" : fmt(parte.gamma_flip, casas)}</b></span>
      <span class="met">Eficiência <b>${er == null ? "–" : fmt(er * 100, 0) + "%"}</b></span>
    </div>`;
  }

  function detalheHtml(z) {
    const casas = CASAS[ativoSel];
    const g = z.gatilho || {};
    const m = g.metricas || {};
    const est = (estado.estudo_regioes || {});
    const numOu = (v, d) => (v == null ? "–" : fmt(v, d));
    const eventos = (m.wyckoff_eventos || []).map(ev => ev.evento || ev.tipo || "").filter(Boolean);
    const iceberg = (m.iceberg || []).map(i => `${esc(i.agente)} ${fmt(i.lote, 0)}×${fmt(i.repeticoes, 0)}`);
    const st = STATUS[g.status] || ["", g.status || "–"];
    return `<h4><span>Zona ${esc(z.id_zona)} · ${fmt(z.preco_centro, casas)} · força ${fmt(z.forca, 0)}</span>
        <button type="button" class="link-fechar" data-fechar="1" style="font:inherit;border:0;background:none;color:var(--s1);cursor:pointer">fechar</button></h4>
      <div class="plano">
        <div><div class="rot">Entrada</div><div class="val">${numOu(z.entrada_min, casas)} – ${numOu(z.entrada_max, casas)}</div></div>
        <div><div class="rot">Stop</div><div class="val">${numOu(z.stop, casas)}</div></div>
        <div><div class="rot">Alvo 1</div><div class="val">${numOu(z.alvo_1, casas)}</div></div>
        <div><div class="rot">Alvo 2</div><div class="val">${numOu(z.alvo_2, casas)}</div></div>
        <div><div class="rot">R:R líquido</div><div class="val">${numOu(z.rr, 2)}</div></div>
        <div><div class="rot">Gatilho</div><div class="val">${esc(st[1])}</div></div>
      </div>
      <div class="metricas">
        <span>Absorção: <b>${m.absorcao ? "sim" : m.absorcao === false ? "não" : "–"}</b>
          (${numOu(m.vol_contra_5min, 0)} contra p80 ${numOu(m.p80_blocos_5min, 0)}; desloc. ${numOu(m.deslocamento_5min, 1)} pts)</span>
        <span>OFI: <b>${numOu(m.ofi_atual, 2)}</b> (anterior ${numOu(m.ofi_anterior, 2)})</span>
        <span>Iceberg: <b>${iceberg.length ? iceberg.join("; ") : "não"}</b></span>
        <span>Wyckoff: <b>${eventos.length ? esc(eventos.join(", ")) : "sem evento"}</b></span>
        <span>Fonte da absorção: ${esc(m.fonte_absorcao || "–")} · ATR 15m ${numOu(m.atr15, 0)}</span>
      </div>
      <div class="nota">Entrada, stop e alvos são o plano da heurística de regiões (custo de 2 lados descontado).</div>
      <div class="nota-estudo">${esc(est.texto || "heurística não validada")}${est.detalhe ? ` — ${esc(est.detalhe)}` : ""}</div>`;
  }

  function regua(e) {
    const parte = (e.regioes || {})[ativoSel] || {};
    const casas = CASAS[ativoSel];
    const estTxt = (e.estudo_regioes || {}).texto || "heurística não validada";
    $("#regua-sub").textContent = `${estTxt}${parte.origem_quantis ? ` · quantis: ${parte.origem_quantis}` : ""}`;
    if (parte.erro && !(parte.zonas || []).length) {
      $("#regua").innerHTML = erroDe(parte);
      return;
    }
    const zonas = parte.zonas || [];
    const acima = zonas.filter(z => z.lado === "ACIMA");
    const abaixo = zonas.filter(z => z.lado === "ABAIXO");
    $("#regua").innerHTML = erroDe(parte) + acima.map(zonaHtml).join("") + linhaPreco(parte) +
      (abaixo.length ? abaixo.map(zonaHtml).join("") : `<div class="vazio">sem suporte com força ≥ 35</div>`);
    if (!acima.length && !zonas.length) {
      $("#regua").innerHTML = erroDe(parte) || `<div class="vazio">sem zonas com força ≥ 35 no estado</div>`;
    }
    // grafico do dia no proprio cartao (antes da regua)
    const card = $("#regua").closest(".card");
    let g = $("#g-preco");
    if (!g && card) {
      g = document.createElement("div");
      g.className = "graf-v3";
      g.id = "g-preco";
      card.insertBefore(g, $("#regua"));
    }
    const serie = parte.serie || {};
    if (g) {
      if ((serie.t || []).length) {
        try {
          V2.grafico("g-preco", serie.t, [{ nome: NOME_ATIVO[ativoSel], v: serie.p }],
            { altura: 110, dec: casas, decEixo: casas });
        } catch (err) {
          g.innerHTML = `<div class="vazio">gráfico indisponível (${esc(err && err.message ? err.message : err)})</div>`;
        }
      } else {
        g.innerHTML = `<div class="vazio">sem série do dia para ${esc(NOME_ATIVO[ativoSel])}</div>`;
      }
    }
    const det = $("#detalhe-zona");
    const z = zonas.find(x => x.id_zona === detalheZona);
    if (z) {
      det.hidden = false;
      det.innerHTML = detalheHtml(z);
    } else {
      det.hidden = true;
      det.innerHTML = "";
    }
    const est = (e.estudo_regioes || {}).texto || "heurística não validada";
    $("#nota-estudo").textContent = est;
  }

  function quantis(e) {
    const q = e.quantis || {};
    if (q.erro) { $("#quantis").innerHTML = erroDe(q); return; }
    const casas = CASAS["WIN$"];
    const bloco = (rot, obj) => {
      if (!obj) return "";
      const itens = [["q05", "5%"], ["q15", "15%"], ["q50", "50%"], ["q85", "85%"], ["q95", "95%"]]
        .map(([k, r]) => `<div><div class="rot">${r}</div><div class="val">${obj[k] == null ? "–" : fmt(obj[k], casas)}</div></div>`)
        .join("");
      return `<div class="unid" style="margin:0 0 3px">${rot} · sigma ${fmt(obj.sigma_prev, 2)}% · slot ${esc(obj.slot || "–")}</div>` +
        `<div class="quantis">${itens}</div>`;
    };
    $("#quantis").innerHTML = (bloco("até o fechamento", q.fechamento) || bloco("60 minutos", q["60min"])) +
      `<div class="fonte">modelo_regime_v3 ${esc(q.modelo || "")} (CQR): cobertura fora da amostra ` +
      `69%/91% (60 min) e 73%/92% (fechamento).</div>`;
  }

  // ---------------------------------------------------------------- direita
  function opcoes(e) {
    const o = e.opcoes || {};
    if (o.erro) { $("#painel-opcoes-conteudo").innerHTML = erroDe(o); return; }
    const d = o.diagnostico || {};
    const pct = v => (num(v) == null ? "–" : fmt(v * 100, 1) + "%");
    const rp = v => (num(v) == null ? "–" : sinal(v * 100, 1) + " p.p.");
    const diag = [
      ["IV ATM", pct(d.iv_atm), `percentil ${d.iv_atm_pct == null ? "–" : fmt(d.iv_atm_pct, 0)}`],
      ["VRP", rp(d.vrp), `RV20 ${pct(d.rv20)}`],
      ["RR25", d.rr25 == null ? "–" : sinal(d.rr25, 3), `percentil ${d.rr25_pct == null ? "–" : fmt(d.rr25_pct, 0)}`],
      ["Skew", esc(d.skew_obs || o.skew || "–"), ""],
      ["GEX", d.gex == null ? "–" : `${sinal(d.gex / 1e6, 0)} mi`, esc(d.gex_sinal || "")],
      ["Gamma flip", d.gamma_flip == null ? "–" : fmt(d.gamma_flip, 2), ""],
      ["Put wall", d.put_wall == null ? "–" : fmt(d.put_wall, 2), ""],
      ["Call wall", d.call_wall == null ? "–" : fmt(d.call_wall, 2), ""],
      ["Max pain", d.max_pain == null ? "–" : fmt(d.max_pain, 2), ""],
    ].map(([r, v, e2]) => `<div><div class="rot">${esc(r)}</div><div class="val">${v}</div>` +
      `${e2 ? `<div class="rot">${e2}</div>` : ""}</div>`).join("");

    const card = (x, papel) => {
      if (!x) return "";
      const pernas = (x.pernas || []).map(p =>
        `<div class="perna"><span>${esc(p.tipo)} ${esc(p.serie || "")} K ${fmt(p.strike, 2)} ` +
        `(${esc(String(p.vencimento || "").slice(5))})</span><b>${fmt(p.mid, 2)}</b></div>`).join("");
      const bes = (x.breakevens || []).map(v => fmt(v, 2)).join(" / ") || "–";
      const nota = (x.notas || []).filter(Boolean).map(n => `<div class="nota">${esc(n)}</div>`).join("");
      return `<div class="estrutura ${papel === "contra" ? "contra" : ""}">
        <h4>${esc(x.nome || x.chave || "–")} <span class="rot" style="font-weight:400;color:var(--muted)">${esc(x.liquidez || "")}</span></h4>
        ${x.motivo ? `<div class="motivo">${esc(x.motivo)}</div>` : ""}
        <div class="pernas">${pernas}</div>
        <div class="resumo">
          <span>custo ${x.custo_mid == null ? "–" : fmt(x.custo_mid, 2)}</span>
          <span>breakeven ${bes}</span>
          <span>ganho ${x.ganho_max == null ? "–" : fmt(x.ganho_max, 2)}</span>
          <span>perda ${x.perda_max == null ? "–" : fmt(x.perda_max, 2)}</span>
          <span>R:R ${x.retorno_risco == null ? "–" : fmt(x.retorno_risco, 2)}</span>
        </div>${nota}</div>`;
    };
    const lista = papel => (o[papel] || []).map(x => card(x, papel)).join("") ||
      `<div class="vazio">sem estrutura ${papel === "contra" ? "contra" : "a favor"} para o cenário</div>`;
    $("#painel-opcoes-conteudo").innerHTML =
      erroDe(o) +
      `<div class="diag">${diag}</div>` +
      `<div class="unid">${esc(o.direcao || "")} · ${esc(o.nivel_iv || "")} · ${esc(o.nota || o.motivo || "")}</div>` +
      `<h4>A favor da tendência</h4><div class="estruturas">${lista("a_favor")}</div>` +
      `<h4 style="margin-top:10px">Contra a tendência</h4><div class="estruturas">${lista("contra")}</div>` +
      `<div class="fonte">superfície do BOVA11 (${esc(String(d.posicoes_fonte || "posição D-1"))}, ref. ${esc(String(d.posicoes_ref || "–"))})</div>`;
  }

  function swing(e) {
    const sw = e.swing || {};
    const bloco = ativo => {
      const zs = ((sw[ativo] || {}).zonas || []);
      if (!zs.length) return "";
      return `<h4>${ativo}</h4><div class="swing-zonas">` + zs.map(z =>
        `<div class="swing-zona" title="${esc((z.componentes || []).join(" · "))}">
          <span>${esc(z.direcao)} ${esc(ROTULO_ALINHAMENTO[z.alinhamento] || z.alinhamento)}</span>
          <span class="p">${fmt(z.preco_centro, CASAS[ativo] ?? 2)} · força ${fmt(z.forca, 0)}</span>
        </div>`).join("") + `</div>`;
    };
    const inv = (sw.invalidacao || []).map(f => `<li>${esc(f)}</li>`).join("");
    $("#painel-swing-conteudo").innerHTML = erroDe(sw) +
      (bloco("WIN$") + bloco("WDO$") || `<div class="vazio">sem zonas de swing no estado</div>`) +
      `<h4 style="margin-top:10px">Invalidação da tese</h4><ul class="invalidacao">` +
      (inv || `<li class="nota">sem frases de invalidação</li>`) + `</ul>`;
  }

  function correlacoes(e) {
    const c = e.correlacoes || {};
    const p = e.players || {};
    const pares = (c.pares || []);
    const alertas = (c.alertas || []);
    const linhas = pares.map(x =>
      `<div class="corr-linha ${x.alerta ? "alerta" : ""}">
        <span>${esc(V2.parNome(x.y, x.x))}${x.alerta ? " · descolamento" : ""}</span>
        <span class="${clsCor(x.corr)}">${sinal(x.corr, 2)}</span>
        <span title="q de FDR">${x.q_valor == null ? "–" : fmt(x.q_valor, 3)}</span>
      </div>`).join("");
    const grupos = (p.grupos || []).map(g =>
      `<div class="players-linha"><span class="g">${esc(g.grupo)}</span>` +
      `<b class="num-g">${fmt(g.saldo, 0)}</b></div>`).join("");
    const fonte = p.erro ? erroDe(p) : `<div class="fonte">Saldo final do dia por grupo em ${esc(p.ativo || "–")} ` +
      `(${esc(p.unidade || "")}), fonte <b>${esc(p.fonte_saldo || "–")}</b>` +
      `${p.fonte_saldo === "FITA_FALLBACK" ? ": saldo reconstruído pela fita (forma boa, nível aproximado)" : ""}` +
      `${p.captura_media != null ? ` · captura média ${fmt(p.captura_media * 100, 0)}%` : ""}.</div>`;
    const fl = e.fluxo || {};
    const now = fl.nowcast || {};
    const of = fl.oficial || {};
    const fluxoTxt = now.estimativa_mi != null
      ? `Nowcast do estrangeiro hoje: <b>${fmt(now.estimativa_mi / 1000, 2)} R$ bi</b> (${fmt(now.n_obs, 0)} observações)`
      : `Nowcast do estrangeiro: ${esc(now.erro || "sem componentes")}`;
    const oficialTxt = of.ultimo
      ? ` · oficial D+2 de ${V2.dataBRCurta(of.ultimo.data_ref)}: ${sinal(of.ultimo.saldo_mi / 1000, 2)} R$ bi` +
        `${of.cinco_dias_mi != null ? ` (5 pregões ${sinal(of.cinco_dias_mi / 1000, 2)} R$ bi)` : ""}`
      : "";
    $("#painel-corr-conteudo").innerHTML = erroDe(c) +
      `<div class="unid">janela ${esc(V2.janelaNome(c.janela))} · ${fmt(c.n_minutos, 0)} min · ` +
      `FDR ${fmt(c.fdr_alpha, 2)} em ${fmt(c.n_hipoteses, 0)} hipóteses · regime ${esc((c.regime || {}).balde || "–")}</div>` +
      `<div class="corr-lista">${linhas || `<div class="vazio">sem pares no estado</div>`}</div>` +
      `<div class="nota">${alertas.length} par(es) com alerta de FDR${alertas.length ? ": " +
        esc(alertas.map(a => `${V2.parNome(a.y, a.x)}`).join(", ")) : ""}.</div>` +
      `<div class="nota">${fluxoTxt}${oficialTxt}</div>` +
      `<h4 style="margin-top:10px">Placar dos grupos no WIN</h4>` +
      `<div class="players-lista">${grupos || `<div class="vazio">sem saldo do dia</div>`}</div>${fonte}`;
  }

  // ---------------------------------------------------------------- render
  function render(e) {
    estado = e;
    cabecalho(e);
    briefing(e);
    pilares(e);
    abertura(e);
    risco(e);
    globalBloco(e);
    regua(e);
    quantis(e);
    opcoes(e);
    swing(e);
    correlacoes(e);
    $("#aba-grafico").href = `/grafico?ativo=${ativoSel === "WDO$" ? "WDO" : "WIN"}`;
  }

  // ---------------------------------------------------------------- eventos
  function selecionarAtivo(a) {
    ativoSel = a;
    detalheZona = null;
    document.querySelectorAll("#sel-ativo button").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset.ativo === a)));
    if (estado) render(estado);
  }

  function selecionarAba(aba) {
    document.querySelectorAll("#abas-mobile button").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset.aba === aba)));
    document.body.dataset.aba = aba;
    // o grafico nasce escondido (largura 0): ao abrir a Regua, redesenha com a largura real
    if (aba === "regua" && estado && window.V2 && V2.redesenhar) {
      requestAnimationFrame(() => V2.redesenhar());
    }
  }

  function selecionarAbaDir(aba) {
    abaDir = aba;
    document.querySelectorAll("#abas-dir button").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset.abaDir === aba)));
    $("#painel-opcoes-conteudo").hidden = aba !== "opcoes";
    $("#painel-swing-conteudo").hidden = aba !== "swing";
    $("#painel-corr-conteudo").hidden = aba !== "corr";
  }

  document.addEventListener("click", ev => {
    const ativo = ev.target.closest("[data-ativo]");
    if (ativo && ativo.closest("#sel-ativo")) return selecionarAtivo(ativo.dataset.ativo);
    const aba = ev.target.closest("button[data-aba]");
    if (aba) return selecionarAba(aba.dataset.aba);
    const ad = ev.target.closest("button[data-aba-dir]");
    if (ad) return selecionarAbaDir(ad.dataset.abaDir);
    if (ev.target.closest("[data-fechar]")) {
      detalheZona = null;
      if (estado) render(estado);
      return;
    }
    const zona = ev.target.closest(".zona");
    if (zona) {
      detalheZona = detalheZona === zona.dataset.id ? null : zona.dataset.id;
      if (estado) render(estado);
    }
  });

  async function atualizar() {
    const r = await fetch("/api/v3/cockpit", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const e = await r.json();
    recebidoEm = Date.now();
    render(e);
  }

  V2.nav("/v3");
  document.body.dataset.aba = "cenario";
  selecionarAbaDir("opcoes");
  V2.laco(atualizar, 5000);
})();
