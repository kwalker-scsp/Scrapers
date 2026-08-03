/* Review dashboard for the submission queue.
 *
 * Diff rendering note: added/removed use green/red, but that pair is
 * indistinguishable under deuteranopia (measured ΔE 4.1). Every row therefore
 * carries a "was:"/"now:" label and a ±  glyph, and removed values are struck
 * through — colour is only ever reinforcement here.
 */
(function () {
  "use strict";

  var API = "/api/v1";
  var PAGE_SIZE = 25;

  var EDITABLE = [
    "date_text", "date_start", "date_end", "date_precision",
    "section", "subsection", "body", "tags", "research_categories"
  ];
  var PRECISIONS = [
    "day", "month", "month-range", "season", "year", "year-range", "range", "approx", "undated"
  ];

  var state = { key: null, actor: "", offset: 0, items: [], total: 0 };

  function $(id) { return document.getElementById(id); }

  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (kid) {
      if (kid === null || kid === undefined) return;
      node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
    return node;
  }

  function headers() {
    return { "X-API-Key": state.key, "X-Actor": state.actor || "unnamed", "Content-Type": "application/json" };
  }

  async function api(path, options) {
    var opts = options || {};
    var res = await fetch(API + path, {
      method: opts.method || "GET",
      headers: headers(),
      body: opts.body ? JSON.stringify(opts.body) : undefined
    });
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
    if (!res.ok) {
      var err = new Error((data && data.detail) ? formatDetail(data.detail) : "HTTP " + res.status);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function formatDetail(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map(function (d) {
        return (d.loc ? d.loc.join(".") + ": " : "") + (d.msg || JSON.stringify(d));
      }).join("; ");
    }
    return JSON.stringify(detail);
  }

  /* Renders a field value the way a reviewer needs to read it: lists as
     comma-separated labels, source objects with their URLs, null as an explicit
     "(empty)" rather than a blank cell. */
  function showValue(field, value) {
    if (value === null || value === undefined || value === "") {
      return el("em", { class: "muted", text: "(empty)" });
    }
    if (field === "sources" && Array.isArray(value)) {
      return el("span", {}, value.map(function (s, i) {
        return el("span", {}, [
          i ? "; " : "",
          s.name || "?",
          s.url ? el("span", { class: "muted mono", text: " " + s.url }) : null
        ]);
      }));
    }
    if (Array.isArray(value)) return el("span", { text: value.length ? value.join(", ") : "(none)" });
    if (typeof value === "object") return el("span", { class: "mono", text: JSON.stringify(value) });
    return el("span", { text: String(value) });
  }

  // ---- diff / preview ------------------------------------------------------

  function renderDiff(sub) {
    var table = el("table", { class: "diff" }, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: "Field" }),
        el("th", { text: "Proposed change" })
      ])])
    ]);
    var body = el("tbody", {});
    sub.diff.forEach(function (row) {
      var cells = [
        el("span", { class: "val before" }, [
          el("span", { class: "val__label", text: "was:" }),
          el("del", {}, [showValue(row.field, row.before)])
        ]),
        el("span", { class: "val after" }, [
          el("span", { class: "val__label", text: "now:" }),
          showValue(row.field, row.after)
        ])
      ];
      if (row.conflict) {
        cells.push(el("div", { class: "notice notice--warn", style: "margin:8px 0 0;" }, [
          el("strong", { text: "⚠ Conflict — " }),
          "the published value changed after this was submitted. It is now: ",
          showValue(row.field, row.current),
          ". Approving replaces that newer value."
        ]));
      }
      body.appendChild(el("tr", { class: row.conflict ? "conflict" : "" }, [
        el("td", { class: "field", text: row.field }),
        el("td", {}, cells)
      ]));
    });
    table.appendChild(body);
    return table;
  }

  function renderPreview(sub) {
    var p = sub.preview || {};
    var dl = el("dl", { class: "kv", style: "margin-top:8px;" });
    ["date_text", "date_start", "date_end", "date_precision", "section", "subsection",
      "tags", "research_categories", "sources"].forEach(function (f) {
      if (p[f] === undefined) return;
      dl.appendChild(el("dt", { text: f }));
      dl.appendChild(el("dd", {}, [showValue(f, p[f])]));
    });
    return el("div", {}, [
      el("p", { style: "margin:8px 0 0;font-size:14.5px;", text: p.body || "" }),
      dl
    ]);
  }

  // ---- edit-then-approve editor -------------------------------------------

  function buildEditor(sub) {
    /* Pre-populates from the proposal (for a new event) or from the proposed
       patch (for an edit). Only fields the reviewer actually changes are sent as
       `overrides`, so an untouched field stays exactly as the scraper proposed
       it and the audit log stays honest about what was reviewer-authored. */
    var base = sub.submission_type === "new" ? (sub.preview || {}) : (sub.proposed_patch || {});
    var host = el("div", { class: "editor" });
    host.appendChild(el("div", { class: "muted", style: "font-size:12.5px;", text:
      sub.submission_type === "new"
        ? "Change any field, then Approve. Only edited fields are recorded as reviewer overrides."
        : "These are the fields this edit proposes. Adjust the new values, then Approve." }));

    var grid = el("div", { class: "editor__grid" });
    var inputs = {};

    EDITABLE.forEach(function (field) {
      if (sub.submission_type === "edit" && base[field] === undefined) return;

      var initial = base[field];
      var wrap = el("div", {});
      wrap.appendChild(el("label", { for: "ed-" + sub.id + "-" + field, text: field }));

      var input;
      if (field === "body") {
        input = el("textarea", { id: "ed-" + sub.id + "-" + field, rows: "4" });
        input.value = initial || "";
      } else if (field === "date_precision") {
        input = el("select", { id: "ed-" + sub.id + "-" + field });
        PRECISIONS.forEach(function (p) {
          input.appendChild(el("option", { value: p, text: p, selected: p === initial ? "selected" : null }));
        });
      } else if (field === "date_start" || field === "date_end") {
        input = el("input", { type: "date", id: "ed-" + sub.id + "-" + field });
        input.value = initial || "";
      } else {
        input = el("input", { type: "text", id: "ed-" + sub.id + "-" + field });
        input.value = Array.isArray(initial) ? initial.join(", ") : (initial || "");
        if (field === "tags" || field === "research_categories") {
          input.placeholder = "comma-separated";
        }
      }
      input.dataset.original = JSON.stringify(initial === undefined ? null : initial);
      inputs[field] = input;
      wrap.appendChild(input);
      (field === "body" ? host : grid).appendChild(wrap);
    });

    host.appendChild(grid);

    host.collectOverrides = function () {
      var out = {};
      Object.keys(inputs).forEach(function (field) {
        var input = inputs[field];
        var original = JSON.parse(input.dataset.original);
        var value;
        if (field === "tags" || field === "research_categories") {
          value = input.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        } else {
          value = input.value.trim();
          if (value === "") value = null;
        }
        var same = JSON.stringify(value) === JSON.stringify(original) ||
          (Array.isArray(value) && Array.isArray(original) &&
            value.join("|") === original.join("|"));
        if (!same) out[field] = value;
      });
      return out;
    };
    return host;
  }

  // ---- card ----------------------------------------------------------------

  function card(sub) {
    var node = el("div", { class: "card" + (sub.has_conflict ? " card--conflict" : ""), id: "sub-" + sub.id });
    var distinctScrapers = {};
    (sub.evidence || []).forEach(function (e) { distinctScrapers[e.scraper] = true; });
    var distinctCount = Object.keys(distinctScrapers).length;

    node.appendChild(el("div", { class: "card__head" }, [
      el("span", {
        class: "badge " + (sub.submission_type === "new" ? "badge--new" : "badge--edit"),
        text: sub.submission_type === "new" ? "＋ New event" : "✎ Edit event #" + sub.target_event_id
      }),
      el("h3", { text: "#" + sub.id }),
      el("span", { class: "badge", text: sub.status }),
      sub.has_conflict ? el("span", { class: "badge badge--warn", text: "⚠ Conflict" }) : null,
      sub.edited_by_reviewer ? el("span", { class: "badge", text: "✎ reviewer-edited" }) : null,
      el("span", { class: "spacer", style: "flex:1 1 auto;" }),
      el("span", { class: "muted", style: "font-size:12.5px;", text: String(sub.submitted_at).replace("T", " ").slice(0, 16) })
    ]));

    var kv = el("dl", { class: "kv" });
    function row(k, v) {
      if (v === null || v === undefined || v === "") return;
      kv.appendChild(el("dt", { text: k }));
      kv.appendChild(typeof v === "string" ? el("dd", { text: v }) : el("dd", {}, [v]));
    }
    row("Scraper", sub.scraper + (sub.scraper_run_id ? " (run " + sub.scraper_run_id + ")" : ""));
    // Distinct-scraper count matters more than the raw total: 5 reports from one
    // looping scraper is not the same evidence as 2 from two independent ones.
    row("Corroboration", sub.corroboration_count + " report(s) from " + distinctCount +
      " distinct scraper" + (distinctCount === 1 ? "" : "s") +
      ((sub.evidence || []).map(function (e) { return e.scraper; }).join(", ")
        ? " — " + Object.keys(distinctScrapers).join(", ") : ""));
    if (sub.confidence !== null && sub.confidence !== undefined) {
      row("Scraper confidence", String(sub.confidence));
    }
    row("Notes", sub.notes);
    if (sub.decision_reason) row("Decision reason", sub.decision_reason);
    if (sub.decided_by) row("Decided by", sub.decided_by + " at " + String(sub.decided_at).replace("T", " ").slice(0, 16));
    if (sub.resulting_event_id) {
      row("Resulting event", el("a", { href: "/#event-" + sub.resulting_event_id, text: "#" + sub.resulting_event_id }));
    }
    row("Dedup key", el("span", { class: "mono muted", text: sub.dedup_key.slice(0, 20) + "…" }));
    node.appendChild(kv);

    (sub.warnings || []).forEach(function (w) {
      node.appendChild(el("div", { class: "notice notice--warn", style: "margin-top:10px;", text: "⚠ " + w }));
    });

    // Cited sources for a new event — the reviewer's main quality signal.
    if (sub.submission_type === "new") {
      node.appendChild(el("h4", { style: "margin:14px 0 0;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);", text: "Full preview" }));
      node.appendChild(renderPreview(sub));
    } else if (sub.diff && sub.diff.length) {
      node.appendChild(el("h4", { style: "margin:14px 0 0;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);", text: "Proposed changes" }));
      node.appendChild(renderDiff(sub));
    } else {
      node.appendChild(el("div", { class: "notice", style: "margin-top:10px;", text: "No field changes recorded for this submission." }));
    }

    if ((sub.evidence || []).length > 1) {
      var ev = el("details", { style: "margin-top:10px;" });
      ev.appendChild(el("summary", { style: "cursor:pointer;font-size:13px;", text: "Corroborating reports (" + sub.evidence.length + ")" }));
      sub.evidence.forEach(function (e) {
        ev.appendChild(el("div", { style: "font-size:13px;padding:6px 0;border-top:1px solid var(--gridline);" }, [
          el("strong", { text: e.scraper }),
          el("span", { class: "muted", text: " " + String(e.received_at).replace("T", " ").slice(0, 16) }),
          e.source_summary ? el("div", { class: "muted", style: "font-size:12.5px;", text: e.source_summary }) : null
        ]));
      });
      node.appendChild(ev);
    }

    if (sub.status !== "pending") return node;

    // --- actions ------------------------------------------------------------
    var msg = el("div", { style: "margin-top:10px;" });
    var editorHost = el("div", {});
    var editor = null;

    var actions = el("div", { class: "card__actions" });
    var reason = el("input", { type: "text", placeholder: "Reason (optional, recorded in the audit log)", style: "flex:1 1 240px;" });

    async function decide(kind, extra) {
      var buttons = actions.querySelectorAll("button");
      buttons.forEach(function (b) { b.disabled = true; });
      msg.innerHTML = "";
      var payload = Object.assign({ reason: reason.value.trim() || null }, extra || {});
      try {
        var out = await api("/review/submissions/" + sub.id + "/" + kind, { method: "POST", body: payload });
        node.classList.add("done");
        msg.appendChild(el("div", { class: "notice notice--ok", text:
          kind === "approve"
            ? "Approved. Published event #" + out.event_id + " is now at version " + out.event_version +
              " (" + Object.keys(out.applied_changes || {}).length + " field(s) applied)."
            : "Rejected and archived for audit." }));
        actions.remove();
        editorHost.remove();
        await refreshStats();
      } catch (err) {
        buttons.forEach(function (b) { b.disabled = false; });
        var box = el("div", { class: "notice notice--error" }, [el("strong", { text: "Could not " + kind + ": " }), err.message]);
        if (err.status === 409 && kind === "approve" && !(extra || {}).force) {
          box.appendChild(el("div", { style: "margin-top:8px;" }, [
            el("button", {
              type: "button",
              class: "danger",
              text: "Approve anyway (force)",
              onclick: function () { decide("approve", Object.assign({ force: true }, extra || {})); }
            })
          ]));
        }
        msg.appendChild(box);
      }
    }

    actions.appendChild(el("button", {
      type: "button", class: "primary", text: "✓ Approve",
      onclick: function () { decide("approve"); }
    }));
    actions.appendChild(el("button", {
      type: "button", text: "✎ Edit, then approve",
      onclick: function () {
        if (!editor) {
          editor = buildEditor(sub);
          editorHost.appendChild(editor);
          editorHost.appendChild(el("div", { style: "margin-top:10px;" }, [
            el("button", {
              type: "button", class: "primary", text: "✓ Approve with my edits",
              onclick: function () {
                var overrides = editor.collectOverrides();
                if (!Object.keys(overrides).length) {
                  msg.innerHTML = "";
                  msg.appendChild(el("div", { class: "notice", text: "Nothing was changed — use plain Approve instead." }));
                  return;
                }
                decide("approve", { overrides: overrides });
              }
            })
          ]));
        } else {
          editorHost.classList.toggle("hidden");
        }
      }
    }));
    actions.appendChild(el("button", {
      type: "button", class: "danger", text: "✕ Reject",
      onclick: function () { decide("reject"); }
    }));
    actions.appendChild(reason);

    node.appendChild(actions);
    node.appendChild(editorHost);
    node.appendChild(msg);
    return node;
  }

  // ---- queue ---------------------------------------------------------------

  async function refreshStats() {
    try {
      var s = await api("/review/stats");
      var counts = s.counts_by_status || {};
      var host = $("stats");
      host.innerHTML = "";
      [
        ["Pending", counts.pending || 0],
        ["With conflicts", s.pending_with_conflicts || 0],
        ["Approved", counts.approved || 0],
        ["Rejected", counts.rejected || 0],
        ["Auto-closed", counts.auto_closed || 0],
        ["Published events", s.published_events || 0],
        ["Audit entries", s.audit_entries || 0]
      ].forEach(function (pair) {
        host.appendChild(el("div", {}, [
          el("div", { class: "stat__value", text: String(pair[1]) }),
          el("div", { class: "stat__label", text: pair[0] })
        ]));
      });
      $("queue-count").textContent = (counts.pending || 0) + " pending";
    } catch (e) { /* stats are decorative; the queue below is what matters */ }
  }

  async function loadQueue(append) {
    if (!append) { state.offset = 0; state.items = []; }
    var params = new URLSearchParams({
      status: $("f-status").value,
      order: $("f-order").value,
      limit: String(PAGE_SIZE),
      offset: String(state.offset)
    });
    if ($("f-type").value) params.set("submission_type", $("f-type").value);
    if ($("f-scraper").value.trim()) params.set("scraper", $("f-scraper").value.trim());

    var host = $("queue");
    if (!append) host.innerHTML = el("div", { class: "empty", text: "Loading…" }).outerHTML;

    try {
      var data = await api("/review/submissions?" + params.toString());
      if (!append) host.innerHTML = "";
      state.total = data.total;
      state.offset += data.submissions.length;

      if (!data.submissions.length && !append) {
        host.appendChild(el("div", { class: "empty" }, [
          el("div", { style: "font-size:15px;", text: "Nothing here." }),
          el("div", { style: "margin-top:6px;", text: $("f-status").value === "pending"
            ? "The queue is empty — every submission has been reviewed."
            : "No submissions with this status." })
        ]));
      }

      // Detail fetch per row: the list endpoint omits the raw payload, and the
      // editor needs the proposed patch to pre-populate its fields.
      for (var i = 0; i < data.submissions.length; i++) {
        var sub = data.submissions[i];
        if (sub.submission_type === "edit" && sub.status === "pending") {
          sub.proposed_patch = {};
          sub.diff.forEach(function (d) { sub.proposed_patch[d.field] = d.after; });
        }
        host.appendChild(card(sub));
      }

      var more = $("more");
      more.innerHTML = "";
      if (state.offset < state.total) {
        more.appendChild(el("button", {
          type: "button",
          text: "Load more (" + state.offset + " of " + state.total + ")",
          onclick: function () { loadQueue(true); }
        }));
      }
    } catch (err) {
      host.innerHTML = "";
      if (err.status === 401) {
        signOut("That key was rejected. Check REVIEW_API_KEY in your .env.");
        return;
      }
      host.appendChild(el("div", { class: "notice notice--error", text: "Could not load the queue: " + err.message }));
    }
  }

  // ---- auth ----------------------------------------------------------------

  function signOut(message) {
    try { sessionStorage.removeItem("ukrtl-review-key"); } catch (e) { /* ignore */ }
    state.key = null;
    $("app").classList.add("hidden");
    $("auth-gate").classList.remove("hidden");
    $("sign-out").classList.add("hidden");
    $("queue-count").textContent = "";
    $("auth-error").innerHTML = "";
    if (message) {
      $("auth-error").appendChild(el("div", { class: "notice notice--error", style: "margin-top:10px;", text: message }));
    }
  }

  async function signIn(key, actor) {
    state.key = key;
    state.actor = actor || "";
    try {
      await api("/review/stats");
    } catch (err) {
      state.key = null;
      $("auth-error").innerHTML = "";
      $("auth-error").appendChild(el("div", { class: "notice notice--error", style: "margin-top:10px;",
        text: err.status === 401 ? "Invalid key." : "Could not reach the API: " + err.message }));
      return false;
    }
    try {
      sessionStorage.setItem("ukrtl-review-key", key);
      sessionStorage.setItem("ukrtl-review-actor", state.actor);
    } catch (e) { /* private mode */ }
    $("auth-gate").classList.add("hidden");
    $("app").classList.remove("hidden");
    $("sign-out").classList.remove("hidden");
    await refreshStats();
    await loadQueue(false);
    return true;
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem("ukrtl-theme"); } catch (e) { /* ignore */ }
    if (saved) document.documentElement.setAttribute("data-theme", saved);
    $("theme-toggle").addEventListener("click", function () {
      var isDark = document.documentElement.getAttribute("data-theme") === "dark" ||
        (!document.documentElement.hasAttribute("data-theme") &&
          window.matchMedia("(prefers-color-scheme: dark)").matches);
      var next = isDark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("ukrtl-theme", next); } catch (e) { /* ignore */ }
    });
  }

  function init() {
    initTheme();
    $("auth-go").addEventListener("click", function () {
      signIn($("auth-key").value.trim(), $("auth-actor").value.trim());
    });
    $("auth-key").addEventListener("keydown", function (e) {
      if (e.key === "Enter") $("auth-go").click();
    });
    $("sign-out").addEventListener("click", function () { signOut(null); });
    ["f-status", "f-type", "f-order"].forEach(function (id) {
      $(id).addEventListener("change", function () { loadQueue(false); });
    });
    $("f-scraper").addEventListener("change", function () { loadQueue(false); });
    $("f-refresh").addEventListener("click", function () { refreshStats(); loadQueue(false); });

    var saved = null, savedActor = "";
    try {
      saved = sessionStorage.getItem("ukrtl-review-key");
      savedActor = sessionStorage.getItem("ukrtl-review-actor") || "";
    } catch (e) { /* ignore */ }
    if (saved) {
      $("auth-actor").value = savedActor;
      signIn(saved, savedActor);
    }
  }

  init();
})();
