(function () {
  "use strict";

  const state = {
    data: null,
    sortKey: "ts",
    sortDir: "desc",
    q: "",
    kind: "",
    reason: "",
  };

  function $(id) {
    return document.getElementById(id);
  }

  function fmtNum(x, digits) {
    if (x === null || x === undefined || x === "") return "—";
    const n = Number(x);
    if (Number.isNaN(n)) return "—";
    return n.toFixed(digits);
  }

  function fmtInt(x) {
    if (x === null || x === undefined || x === "") return "—";
    return String(x);
  }

  function fmtPct(x) {
    if (x === null || x === undefined) return "N/A";
    return (Number(x) * 100).toFixed(1) + "%";
  }

  function fmtPnl(x, na) {
    if (na || x === null || x === undefined) return "N/A";
    const n = Number(x);
    const sign = n > 0 ? "+" : "";
    return sign + n.toFixed(2);
  }

  function kindClass(kind, decision) {
    if (kind === "take" || decision === "take_yes_paper") return "take";
    if (kind === "skip" || decision === "skip") return "skip";
    return "scan";
  }

  function outcomeCell(row) {
    if (row.kind !== "take") return "—";
    const o = (row.outcome || "n/a").toLowerCase();
    if (o === "win") {
      const extra = row.pnl != null ? " · " + fmtPnl(row.pnl, false) : "";
      return '<span class="pill win">win' + extra + "</span>";
    }
    if (o === "loss") {
      const extra = row.pnl != null ? " · " + fmtPnl(row.pnl, false) : "";
      return '<span class="pill loss">loss' + extra + "</span>";
    }
    return '<span class="pill na">N/A</span>';
  }

  function timeCell(row) {
    const et = row.ts_et || "—";
    const utc = row.ts_utc || "";
    return '<div class="time"><strong>' + escapeHtml(et) + "</strong>" + escapeHtml(utc) + "</div>";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderCards(data) {
    const s = data.summary || {};
    const skipBits = Object.entries(s.skips_by_reason || {})
      .map(function (kv) {
        return (
          '<button type="button" class="chip" data-reason="' +
          escapeHtml(kv[0]) +
          '">' +
          escapeHtml(kv[0]) +
          " · " +
          kv[1] +
          "</button>"
        );
      })
      .join("");
    const winDetail = s.pnl_na
      ? "Settlements missing or no settled takes"
      : s.n_wins + " win / " + s.n_losses + " loss · " + s.n_settled_takes + " settled";
    const html = [
      card("Scans", s.n_scans, s.n_runs ? s.n_runs + " run(s)" : "market looks in ledger"),
      card("Paper takes", s.n_paper_takes, s.n_paper_takes ? "take_yes_paper + older calibrated takes" : "none yet"),
      card("Skips", s.n_skips, skipBits ? '<div class="chips">' + skipBits + "</div>" : "no skip rows"),
      card("Win rate", s.pnl_na ? "N/A" : fmtPct(s.win_rate), winDetail),
      card("PnL", fmtPnl(s.pnl, s.pnl_na), s.settlements_available ? "hold-to-settlement, paper YES" : "N/A — no settlements file"),
      card("Last run", shortLast(data), data.dry_run ? "dry-run · never posts orders" : "dry_run flag false in ledger"),
    ].join("");
    $("cards").innerHTML = html;
    $("cards").querySelectorAll(".chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        $("reason").value = btn.getAttribute("data-reason") || "";
        $("kind").value = "skip";
        state.reason = $("reason").value;
        state.kind = "skip";
        renderTable();
      });
    });
  }

  function card(label, value, detail) {
    return (
      '<article class="card"><div class="label">' +
      escapeHtml(label) +
      '</div><div class="value">' +
      value +
      '</div><div class="detail">' +
      (detail || "") +
      "</div></article>"
    );
  }

  function shortLast(data) {
    if (data.last_run_ts_et) return escapeHtml(data.last_run_ts_et.replace(" ET", ""));
    return "—";
  }

  function fillReasons(rows) {
    const sel = $("reason");
    const current = sel.value;
    const reasons = {};
    rows.forEach(function (r) {
      const key = r.reason_key || r.reason;
      if (key) reasons[key] = true;
    });
    const keys = Object.keys(reasons).sort();
    sel.innerHTML = '<option value="">All</option>' + keys.map(function (k) {
      return '<option value="' + escapeHtml(k) + '">' + escapeHtml(k) + "</option>";
    }).join("");
    sel.value = current;
  }

  function filteredRows() {
    const q = state.q.trim().toLowerCase();
    return (state.data.decisions || []).filter(function (row) {
      if (state.kind && row.kind !== state.kind) return false;
      if (state.reason && row.reason !== state.reason && row.reason_key !== state.reason) return false;
      if (!q) return true;
      const blob = [row.ticker, row.reason, row.decision, row.event_ticker, row.outcome].join(" ").toLowerCase();
      return blob.indexOf(q) !== -1;
    });
  }

  function cmp(a, b, key) {
    const av = a[key];
    const bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") return av - bv;
    return String(av).localeCompare(String(bv), undefined, { numeric: true });
  }

  function renderTable() {
    const tbody = $("decisions").querySelector("tbody");
    const rows = filteredRows().slice().sort(function (a, b) {
      const c = cmp(a, b, state.sortKey);
      return state.sortDir === "asc" ? c : -c;
    });
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="10">No rows match these filters.</td></tr>';
    } else {
      tbody.innerHTML = rows
        .map(function (row) {
          const kc = kindClass(row.kind, row.decision);
          return (
            "<tr>" +
            '<td class="ticker">' +
            escapeHtml(row.ticker || "—") +
            "</td>" +
            "<td>" +
            timeCell(row) +
            "</td>" +
            '<td class="num">' +
            fmtNum(row.yes_mid, 3) +
            "</td>" +
            '<td class="num">' +
            fmtNum(row.yes_ask, 3) +
            "</td>" +
            '<td class="num">' +
            fmtNum(row.model_p, 3) +
            "</td>" +
            '<td class="num">' +
            fmtNum(row.edge, 3) +
            "</td>" +
            '<td class="num">' +
            fmtInt(row.clip) +
            "</td>" +
            "<td><span class=\"pill " +
            kc +
            '">' +
            escapeHtml(row.decision || row.kind) +
            "</span></td>" +
            "<td>" +
            escapeHtml(row.reason || "—") +
            "</td>" +
            "<td>" +
            outcomeCell(row) +
            "</td>" +
            "</tr>"
          );
        })
        .join("");
    }
    $("tableHint").textContent = rows.length + " of " + (state.data.decisions || []).length + " rows";
    document.querySelectorAll("#decisions thead th").forEach(function (th) {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.getAttribute("data-key") === state.sortKey) {
        th.classList.add(state.sortDir === "asc" ? "sort-asc" : "sort-desc");
      }
    });
  }

  function renderOverlays(data) {
    const tbody = $("overlays").querySelector("tbody");
    const rows = data.overlays || [];
    if (!rows.length) {
      tbody.innerHTML = "<tr><td colspan=\"7\">No mm_would_skip / mm overlay events in this ledger.</td></tr>";
      $("overlayHint").textContent = "0 overlay events";
      return;
    }
    tbody.innerHTML = rows
      .map(function (row) {
        return (
          "<tr>" +
          '<td class="ticker">' +
          escapeHtml(row.ticker || "—") +
          "</td>" +
          "<td>" +
          timeCell(row) +
          "</td>" +
          '<td class="num">' +
          fmtNum(row.ask, 3) +
          "</td>" +
          '<td class="num">' +
          fmtInt(row.count) +
          "</td>" +
          "<td>" +
          (row.would_skip ? '<span class="pill skip">yes</span>' : "no") +
          "</td>" +
          "<td>" +
          escapeHtml(row.reason || "—") +
          "</td>" +
          "<td>" +
          escapeHtml(row.note || "") +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
    $("overlayHint").textContent = rows.length + " overlay event(s)";
  }

  function bind() {
    $("q").addEventListener("input", function (e) {
      state.q = e.target.value;
      renderTable();
    });
    $("kind").addEventListener("change", function (e) {
      state.kind = e.target.value;
      renderTable();
    });
    $("reason").addEventListener("change", function (e) {
      state.reason = e.target.value;
      renderTable();
    });
    document.querySelectorAll("#decisions thead th").forEach(function (th) {
      th.addEventListener("click", function () {
        const key = th.getAttribute("data-key");
        if (!key) return;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = key;
          state.sortDir = key === "ts" || key === "ticker" ? "desc" : "desc";
        }
        renderTable();
      });
    });
  }

  function paint(data) {
    state.data = data;
    $("dryBadge").textContent = data.dry_run ? "DRY-RUN" : "DRY-RUN FLAG OFF";
    $("lastRun").textContent = data.last_run_ts_et || data.last_run_ts_utc || "—";
    const src = data.source || {};
    $("sourceLabel").textContent = (src.sample ? "sample ledger" : "ledger.jsonl") +
      (src.settlements ? " + settlements" : " · settlements N/A");
    $("generatedAt").textContent = data.generated_at || "—";
    const empty = $("emptyTakes");
    if ((data.summary || {}).n_paper_takes === 0) {
      empty.textContent = data.empty_takes_copy || "No paper takes yet.";
      empty.classList.remove("hidden");
    } else {
      empty.classList.add("hidden");
    }
    renderCards(data);
    fillReasons(data.decisions || []);
    renderTable();
    renderOverlays(data);
  }

  async function loadData() {
    if (window.DASHBOARD_DATA) return window.DASHBOARD_DATA;
    const res = await fetch("data.json", { cache: "no-store" });
    if (!res.ok) throw new Error("Could not load data.json (" + res.status + ")");
    return res.json();
  }

  bind();
  loadData()
    .then(paint)
    .catch(function (err) {
      $("tableHint").textContent = String(err.message || err);
      $("emptyTakes").textContent =
        "Could not load dashboard/data.json. Run python build_dashboard.py or python serve_dashboard.py.";
      $("emptyTakes").classList.remove("hidden");
    });
})();
