/* Public interactive timeline, grouped into one collapsible section per year.
 *
 * Reads only the public endpoints (/api/v1/events, /api/v1/meta), which serve
 * published events exclusively — this page has no way to see the review queue.
 *
 * FILING RULE (option A). Every event appears exactly once, under the calendar
 * year of its `date_start`, so the year counts always sum to the total. An event
 * whose span crosses a New Year additionally carries a "spans 2022–2024" chip.
 * Note this is decided geometrically — start year vs end year — not from
 * date_precision: three `month-range` events cross a year boundary while nine
 * others don't, so keying off precision would mis-file them.
 *
 * Events with no date at all land in a separate "Undated" section at the bottom.
 *
 * FILTERING. The API stays the authority on filter semantics: one request per
 * filter change pulls the whole matching set (217 events today, paginated in
 * case it grows), and the year grouping is applied to the result purely as
 * presentation. Tag-mode, substring and slug-matching rules are never
 * reimplemented here, so they can't drift from the backend.
 */
(function () {
  "use strict";

  var API = "/api/v1";
  var FETCH_PAGE = 1000;   // API ceiling; the loop below handles any total

  /* Plain-English gloss for each date_precision value, shown in the detail
     drawer so a reader knows how much weight a date carries. */
  var PRECISION_HELP = {
    day: "A specific calendar day.",
    month: "Only the month is known; the day is not.",
    "month-range": "Spans a range of months.",
    season: "Reported as a season (e.g. 'summer 2023').",
    year: "Only the year is known.",
    "year-range": "Spans several years.",
    range: "An explicit start-to-end span.",
    approx: "A best-effort anchor date. Treat as approximate.",
    undated: "No date could be assigned to this event."
  };

  var UNDATED_KEY = "undated";

  var state = {
    meta: null,
    /* The year skeleton is built once from the UNFILTERED set, so the list of
       year rows stays put while you type in a filter — only the counts move. */
    skeletonYears: [],
    yearMin: null,
    yearMax: null,
    hasUndated: false,
    byYear: {},        // year -> [event]  (current filtered set)
    undated: [],
    total: 0,
    expanded: new Set(),
    selectedId: null
  };

  // ---- helpers -------------------------------------------------------------

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

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  function fmtDate(iso) {
    if (!iso) return null;
    var parts = iso.split("-");
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return parts[2].replace(/^0/, "") + " " + months[Number(parts[1]) - 1] + " " + parts[0];
  }

  function spanLabel(ev) {
    if (!ev.date_start) return "Undated";
    if (!ev.date_end || ev.date_end === ev.date_start) return fmtDate(ev.date_start);
    return fmtDate(ev.date_start) + " – " + fmtDate(ev.date_end);
  }

  function yearOf(iso) { return iso ? Number(iso.slice(0, 4)) : null; }

  /* The span chip's text, or null when the event sits inside a single year. */
  function spanChip(ev) {
    if (!ev.date_start || !ev.date_end) return null;
    var a = yearOf(ev.date_start), b = yearOf(ev.date_end);
    return b > a ? "spans " + a + "–" + b : null;
  }

  async function getJSON(path, params) {
    var url = new URL(path, window.location.origin);
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return;
      if (Array.isArray(v)) v.forEach(function (item) { url.searchParams.append(k, item); });
      else url.searchParams.set(k, v);
    });
    var res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status + " for " + url.pathname);
    return res.json();
  }

  /* Pull every event matching `params`, following pagination. */
  async function fetchAll(params) {
    var out = [];
    var offset = 0;
    for (;;) {
      var page = await getJSON(API + "/events",
        Object.assign({}, params, { limit: FETCH_PAGE, offset: offset }));
      out = out.concat(page.events);
      offset += page.events.length;
      if (offset >= page.total || !page.events.length) break;
    }
    return out;
  }

  // ---- grouping ------------------------------------------------------------

  function group(events) {
    var byYear = {};
    var undated = [];
    events.forEach(function (ev) {
      if (!ev.date_start) { undated.push(ev); return; }
      var y = yearOf(ev.date_start);
      (byYear[y] = byYear[y] || []).push(ev);
    });
    return { byYear: byYear, undated: undated };
  }

  /* The 2-3 themes on a collapsed row: the year's most common tags. */
  function topTags(events, n) {
    var counts = {};
    events.forEach(function (ev) {
      (ev.tags || []).forEach(function (t) { counts[t] = (counts[t] || 0) + 1; });
    });
    return Object.keys(counts)
      .sort(function (a, b) { return counts[b] - counts[a] || a.localeCompare(b); })
      .slice(0, n)
      .map(function (t) { return { name: t, count: counts[t] }; });
  }

  // ---- query construction --------------------------------------------------

  function selectedTags() {
    return Array.prototype.slice
      .call(document.querySelectorAll('#f-tags .chip[aria-pressed="true"]'))
      .map(function (b) { return b.dataset.slug; });
  }

  function currentQuery() {
    return {
      q: $("f-q").value.trim().length >= 2 ? $("f-q").value.trim() : null,
      section: $("f-section").value || null,
      research_category: $("f-rc").value ? [$("f-rc").value] : null,
      source: $("f-source").value || null,
      tag: selectedTags(),
      tag_mode: $("f-tagmode").value,
      date_from: $("f-from").value || null,
      date_to: $("f-to").value || null,
      date_precision: $("f-exact").checked ? ["day"] : null,
      // Undated events have their own section, so they are always fetched and
      // the old "include undated" toggle is gone.
      include_undated: "true",
      order: "asc",     // chronological; the year grouping supplies the structure
      limit: FETCH_PAGE,
      offset: 0
    };
  }

  function filtersActive() {
    return !!($("f-q").value.trim() || $("f-section").value || $("f-rc").value ||
      $("f-source").value || selectedTags().length || $("f-exact").checked ||
      $("f-from").value || $("f-to").value);
  }

  // ---- expanded-section state ----------------------------------------------

  /* Derived from the system clock, never hardcoded. If the calendar year has no
     events yet — early January, before the first entry of the new year lands —
     fall back to the most recent year that does. */
  function currentYear() {
    var now = new Date().getFullYear();
    if (state.skeletonYears.indexOf(now) !== -1) return now;
    return state.skeletonYears.length ? state.skeletonYears[state.skeletonYears.length - 1] : null;
  }

  function readHash() {
    var raw = window.location.hash.replace(/^#/, "").trim();
    if (!raw) return null;
    var keys = raw.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    var out = new Set();
    keys.forEach(function (k) {
      if (k === UNDATED_KEY) { if (state.hasUndated) out.add(UNDATED_KEY); return; }
      var y = Number(k);
      if (state.skeletonYears.indexOf(y) !== -1) out.add(y);
    });
    return out.size ? out : null;
  }

  /* replaceState rather than assigning location.hash: the URL stays shareable,
     but opening and closing sections doesn't fill the back button with steps.
     Only DELIBERATE toggles are recorded — never the default landing state or a
     filter's auto-expansion. A bare URL therefore keeps meaning "whatever the
     current year is", so a bookmark made today still opens 2027 in 2027. */
  function writeHash() {
    var keys = Array.prototype.slice.call(state.expanded).sort(function (a, b) {
      if (a === UNDATED_KEY) return 1;
      if (b === UNDATED_KEY) return -1;
      return b - a;
    });
    setHash(keys.length ? "#" + keys.join(",") : "");
  }

  function setHash(hash) {
    try {
      history.replaceState(null, "", window.location.pathname + window.location.search + hash);
    } catch (e) { /* file:// or a blocked history API — the UI still works */ }
  }

  // ---- rendering: filters --------------------------------------------------

  function renderFilters(meta) {
    var sectionSel = $("f-section");
    meta.sections.forEach(function (s) {
      sectionSel.appendChild(el("option", { value: s.slug, text: s.name + " (" + s.event_count + ")" }));
    });
    var rcSel = $("f-rc");
    meta.research_categories.forEach(function (c) {
      rcSel.appendChild(el("option", { value: c.slug, text: c.name + " (" + c.event_count + ")" }));
    });
    var srcSel = $("f-source");
    meta.sources.forEach(function (s) {
      srcSel.appendChild(el("option", { value: s.slug, text: s.name + " (" + s.event_count + ")" }));
    });

    var chips = $("f-tags");
    chips.innerHTML = "";
    meta.tags.forEach(function (t) {
      chips.appendChild(el("button", {
        type: "button",
        class: "chip",
        "aria-pressed": "false",
        "data-slug": t.slug,
        onclick: function () {
          this.setAttribute("aria-pressed", this.getAttribute("aria-pressed") === "true" ? "false" : "true");
          reload();
        }
      }, [t.name, el("span", { class: "chip__count", text: String(t.event_count) })]));
    });
  }

  // ---- rendering: events ---------------------------------------------------

  function eventNode(ev) {
    var classes = ["event"];
    if (ev.is_imprecise) classes.push("event--imprecise");
    if (!ev.date_start) classes.push("event--undated");

    var sources = (ev.sources || []).map(function (s) { return s.name; });
    var chip = spanChip(ev);

    return el("li", {
      class: classes.join(" "),
      tabindex: "0",
      role: "button",
      "aria-current": state.selectedId === ev.id ? "true" : "false",
      "data-id": String(ev.id),
      onclick: function () { openDrawer(ev); },
      onkeydown: function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(ev); }
      }
    }, [
      el("div", { class: "rail" }, [
        el("span", { class: "mark" }, [
          el("span", { class: "mark__dot" }),
          el("span", { class: "mark__bar" })
        ]),
        el("span", { class: "rail__date", text: ev.date_text || spanLabel(ev) }),
        // The precision word is always present, so the shape encoding never
        // has to be interpreted unaided.
        el("span", { class: "rail__precision", text: ev.date_precision }),
        chip ? el("span", { class: "tag tag--span", text: chip }) : null
      ]),
      el("div", {}, [
        el("p", { class: "event__body", text: ev.body }),
        el("div", { class: "event__foot" }, [
          el("div", { class: "tagline" },
            (ev.tags || []).map(function (t) { return el("span", { class: "tag", text: t }); })
              .concat((ev.research_categories || []).map(function (t) {
                return el("span", { class: "tag tag--rc", text: t });
              }))
          ),
          sources.length
            ? el("span", { class: "event__sources", text: "Sources: " + sources.join(", ") })
            : el("span", { class: "event__sources muted", text: "No sources recorded" })
        ])
      ])
    ]);
  }

  // ---- rendering: year sections -------------------------------------------

  function yearSection(key, label, events, meterFraction) {
    var open = state.expanded.has(key);
    var themes = topTags(events, 3);

    var summaryKids = [
      el("span", { class: "yearsec__caret", "aria-hidden": "true" }),
      el("span", { class: "yearsec__label", text: label }),
      el("span", { class: "yearsec__themes" }, themes.length
        ? themes.map(function (t, i) {
            return el("span", { class: "yearsec__theme" }, [
              i ? el("span", { class: "yearsec__dot", text: "·" }) : null,
              el("span", { text: t.name }),
              el("span", { class: "muted", text: " " + t.count })
            ]);
          })
        : [el("span", { class: "muted", text: events.length ? "—" : "no matches" })]),
      el("span", { class: "spacer" })
    ];

    if (meterFraction !== null) {
      summaryKids.push(el("span", { class: "yearsec__meter" }, [
        /* Non-zero counts get a 3px floor so a 1-event year can never read as an
           empty track. One series, length only — the count sits beside it. */
        el("span", {
          class: "yearsec__fill",
          style: "width:" + (events.length ? "max(3px," + (meterFraction * 100).toFixed(1) + "%)" : "0")
        })
      ]));
    }
    summaryKids.push(el("span", { class: "yearsec__count", text: plural(events.length, "event") }));

    var details = el("details", {
      class: "yearsec"
        + (events.length ? "" : " yearsec--empty")
        + (key === currentYear() ? " yearsec--current" : ""),
      open: open ? "open" : null
    }, [el("summary", {}, summaryKids)]);

    if (!events.length) {
      details.appendChild(el("div", { class: "empty", text: "No events in " + label + " match the current filters." }));
    } else {
      var list = el("ul", { class: "events" });
      events.forEach(function (ev) { list.appendChild(eventNode(ev)); });
      details.appendChild(list);
    }

    details.addEventListener("toggle", function () {
      if (details.open) state.expanded.add(key); else state.expanded.delete(key);
      writeHash();
    });
    return details;
  }

  function renderYears() {
    var host = $("years");
    host.innerHTML = "";

    if (state.yearMin === null) {
      host.appendChild(el("div", { class: "empty", text: "No published events." }));
      return;
    }

    var counts = state.skeletonYears.map(function (y) {
      return (state.byYear[y] || []).length;
    });
    var max = Math.max.apply(null, counts.concat([1]));

    /* Walk newest → oldest. Years absent from the skeleton (2015–2020 today) are
       collapsed into one muted divider rather than six dead rows. */
    var gap = [];
    function flushGap() {
      if (!gap.length) return;
      var lo = gap[gap.length - 1], hi = gap[0];
      host.appendChild(el("div", {
        class: "yeargap",
        text: (lo === hi ? String(lo) : lo + "–" + hi) + " · no events recorded"
      }));
      gap = [];
    }

    for (var y = state.yearMax; y >= state.yearMin; y--) {
      if (state.skeletonYears.indexOf(y) === -1) { gap.push(y); continue; }
      flushGap();
      var evs = state.byYear[y] || [];
      host.appendChild(yearSection(y, String(y), evs, evs.length / max));
    }
    flushGap();

    if (state.hasUndated) {
      host.appendChild(yearSection(UNDATED_KEY, "Undated", state.undated, null));
    }
  }

  // ---- detail drawer -------------------------------------------------------

  function closeDrawer() {
    $("drawer").dataset.open = "false";
    $("scrim").dataset.open = "false";
    state.selectedId = null;
    document.querySelectorAll('.event[aria-current="true"]').forEach(function (n) {
      n.setAttribute("aria-current", "false");
    });
  }

  function openDrawer(ev) {
    state.selectedId = ev.id;
    document.querySelectorAll(".event").forEach(function (n) {
      n.setAttribute("aria-current", n.dataset.id === String(ev.id) ? "true" : "false");
    });

    var drawer = $("drawer");
    drawer.innerHTML = "";
    drawer.appendChild(el("div", { class: "drawer__head" }, [
      el("h2", { text: ev.date_text || spanLabel(ev) }),
      el("button", { type: "button", class: "ghost", text: "✕", "aria-label": "Close", onclick: closeDrawer })
    ]));

    var chip = spanChip(ev);
    drawer.appendChild(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;" }, [
      el("span", {
        // Glyph + word carry the precision, so this badge needs no colour role.
        class: "badge",
        text: (ev.is_imprecise ? "◌ " : "■ ") + ev.date_precision
      }),
      chip ? el("span", { class: "badge badge--danger", text: chip }) : null,
      el("span", { class: "badge", text: "v" + ev.version }),
      el("span", { class: "badge", text: "#" + ev.id })
    ]));

    drawer.appendChild(el("p", { class: "drawer__body", text: ev.body }));

    var dl = el("dl", {});
    function row(label, value) {
      if (value === null || value === undefined || value === "") return;
      dl.appendChild(el("dt", { text: label }));
      dl.appendChild(typeof value === "string" ? el("dd", { text: value }) : el("dd", {}, [value]));
    }
    row("Resolved dates", spanLabel(ev));
    row("Precision", PRECISION_HELP[ev.date_precision] || ev.date_precision);
    row("Filed under", ev.date_start
      ? yearOf(ev.date_start) + (chip ? " (start year of a multi-year span)" : "")
      : "Undated section");
    row("Section", ev.section);
    row("Subsection", ev.subsection);
    if ((ev.tags || []).length) {
      row("Tags", el("div", { class: "tagline" }, ev.tags.map(function (t) {
        return el("span", { class: "tag", text: t });
      })));
    }
    if ((ev.research_categories || []).length) {
      row("Research categories", el("div", { class: "tagline" }, ev.research_categories.map(function (t) {
        return el("span", { class: "tag tag--rc", text: t });
      })));
    }
    drawer.appendChild(dl);

    drawer.appendChild(el("h3", { class: "drawer__section", text: "Sources" }));
    var list = el("ul", { class: "sources" });
    if (!(ev.sources || []).length) {
      list.appendChild(el("li", { class: "muted", text: "No sources recorded for this event." }));
    }
    (ev.sources || []).forEach(function (s) {
      var kids = [el("strong", { text: s.name })];
      if (s.title) kids.push(el("div", { text: s.title }));
      if (s.url) kids.push(el("div", {}, [el("a", { href: s.url, target: "_blank", rel: "noopener noreferrer", text: s.url })]));
      if (s.quote) kids.push(el("blockquote", { class: "drawer__quote", text: "“" + s.quote + "”" }));
      if (s.accessed_at) kids.push(el("div", { class: "muted", style: "font-size:12px;", text: "Accessed " + s.accessed_at }));
      list.appendChild(el("li", {}, kids));
    });
    drawer.appendChild(list);

    // Provenance: this is a research artifact, so the change history is a
    // first-class part of the record, not an admin detail.
    drawer.appendChild(el("h3", { class: "drawer__section", text: "Provenance" }));
    drawer.appendChild(el("div", { style: "font-size:13px;color:var(--text-secondary);" }, [
      el("div", { text: "Created " + String(ev.created_at).slice(0, 10) + " · last changed " + String(ev.updated_at).slice(0, 10) }),
      ev.seed_id ? el("div", { text: "Seed dataset id " + ev.seed_id }) : null,
      ev.external_id ? el("div", { text: "External id " + ev.external_id }) : null,
      el("div", { class: "mono muted", style: "margin-top:4px;word-break:break-all;", text: "dedup " + ev.dedup_key.slice(0, 16) + "…" })
    ]));

    var histHost = el("div", { style: "margin-top:10px;" });
    histHost.appendChild(el("button", {
      type: "button",
      text: "Show change history",
      onclick: async function () {
        var btn = this;
        btn.disabled = true;
        btn.textContent = "Loading…";
        try {
          var hist = await getJSON(API + "/events/" + ev.id + "/history", { verify: "true" });
          btn.remove();
          histHost.appendChild(renderHistory(hist));
        } catch (err) {
          btn.disabled = false;
          btn.textContent = "Retry";
          histHost.appendChild(el("div", { class: "notice notice--error", text: String(err.message) }));
        }
      }
    }));
    drawer.appendChild(histHost);

    drawer.dataset.open = "true";
    $("scrim").dataset.open = "true";
    drawer.focus();
  }

  function renderHistory(hist) {
    var wrap = el("div", {});
    var v = hist.verification;
    if (v) {
      wrap.appendChild(el("div", {
        class: "notice " + (v.reconstructs_current_state ? "notice--ok" : "notice--error"),
        text: v.reconstructs_current_state
          ? "✓ Verified: replaying " + v.entries_applied + " audit " + (v.entries_applied === 1 ? "entry" : "entries") + " reproduces the current record exactly."
          : "✕ Audit log does not reproduce the current record. Mismatched: " + v.mismatched_fields.join(", ")
      }));
    }
    hist.entries.forEach(function (e) {
      var kids = [
        el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;" }, [
          el("span", { class: "badge", text: e.action }),
          el("span", { class: "muted", style: "font-size:12px;", text: String(e.occurred_at).replace("T", " ").slice(0, 16) }),
          el("span", { class: "muted", style: "font-size:12px;", text: e.actor })
        ])
      ];
      if (e.note) kids.push(el("div", { style: "font-size:13px;margin-top:2px;", text: e.note }));
      if (e.changes) {
        var fields = Object.keys(e.changes).filter(function (f) { return f !== "dedup_key"; });
        kids.push(el("div", { class: "muted", style: "font-size:12px;margin-top:2px;", text: "fields: " + fields.join(", ") }));
      }
      if (e.submission_id) {
        kids.push(el("div", { class: "muted", style: "font-size:12px;", text: "from submission #" + e.submission_id }));
      }
      wrap.appendChild(el("div", { style: "padding:8px 0;border-top:1px solid var(--gridline);" }, kids));
    });
    return wrap;
  }

  // ---- loading -------------------------------------------------------------

  /* Every fetch takes a ticket; only the newest paints. Without this, two quick
     filter edits can land out of order and show the wrong result set. */
  var ticketSeq = 0;

  async function load() {
    var ticket = ++ticketSeq;
    $("result-count").textContent = "loading…";
    try {
      var events = await fetchAll(currentQuery());
      if (ticket !== ticketSeq) return;

      var grouped = group(events);
      state.byYear = grouped.byYear;
      state.undated = grouped.undated;
      state.total = events.length;

      /* With a filter on, open every year that has a hit — otherwise you'd
         search and be left staring at a wall of collapsed rows. With no filter,
         only the current year is open. */
      if (filtersActive()) {
        state.expanded = new Set();
        state.skeletonYears.forEach(function (y) {
          if ((state.byYear[y] || []).length) state.expanded.add(y);
        });
        if (state.undated.length) state.expanded.add(UNDATED_KEY);
      }

      var yearsWithHits = state.skeletonYears.filter(function (y) {
        return (state.byYear[y] || []).length;
      }).length;
      $("result-count").textContent = plural(state.total, "event") + " · " +
        plural(yearsWithHits, "year") + (filtersActive() ? " matched" : "");

      renderYears();
    } catch (err) {
      if (ticket !== ticketSeq) return;
      $("years").innerHTML = "";
      $("years").appendChild(el("div", { class: "notice notice--error", text: "Could not load events: " + err.message }));
      $("result-count").textContent = "error";
    }
  }

  var reloadTimer = null;
  function reload() {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(load, 120);
  }

  /* One unfiltered pass fixes the year skeleton: which years get a row, the
     outer bounds, and whether an Undated section exists at all. */
  async function loadSkeleton() {
    var events = await fetchAll({ include_undated: "true", order: "asc" });
    var grouped = group(events);
    state.skeletonYears = Object.keys(grouped.byYear).map(Number)
      .sort(function (a, b) { return a - b; });
    state.hasUndated = grouped.undated.length > 0;
    state.yearMin = state.skeletonYears.length ? state.skeletonYears[0] : null;
    state.yearMax = state.skeletonYears.length
      ? state.skeletonYears[state.skeletonYears.length - 1] : null;
  }

  // ---- theme ---------------------------------------------------------------

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem("ukrtl-theme"); } catch (e) { /* private mode */ }
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

  // ---- wiring --------------------------------------------------------------

  /* A blank page is the worst possible failure, and the likeliest cause is a
     cached index.html from before this layout existed — the static mount sends
     no Cache-Control, so a soft reload can pair old HTML with new JS. Say so
     out loud instead of dying on a null element. */
  function bailIfStalePage() {
    var required = ["years", "result-count", "f-q", "f-tags"];
    var missing = required.filter(function (id) { return !$(id); });
    if (!missing.length) return false;

    var banner = document.createElement("div");
    banner.className = "notice notice--error";
    banner.style.margin = "20px";
    banner.textContent = "This page is a cached older version (missing: " +
      missing.join(", ") + "). Hard-reload to get the current one — " +
      "⌘⇧R on macOS, Ctrl⇧R elsewhere.";
    (document.querySelector("main") || document.body).prepend(banner);
    return true;
  }

  async function init() {
    if (bailIfStalePage()) return;
    initTheme();

    ["f-section", "f-rc", "f-source", "f-tagmode", "f-from", "f-to", "f-exact"].forEach(function (id) {
      $(id).addEventListener("change", reload);
    });
    $("f-q").addEventListener("input", reload);
    $("f-reset").addEventListener("click", function () {
      ["f-q", "f-section", "f-rc", "f-source", "f-from", "f-to"].forEach(function (id) { $(id).value = ""; });
      $("f-tagmode").value = "any";
      $("f-exact").checked = false;
      document.querySelectorAll("#f-tags .chip").forEach(function (b) { b.setAttribute("aria-pressed", "false"); });
      // Back to the landing state: current year only, and a bare URL again.
      var cy = currentYear();
      state.expanded = cy === null ? new Set() : new Set([cy]);
      setHash("");
      reload();
    });
    $("scrim").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeDrawer();
    });

    try {
      state.meta = await getJSON(API + "/meta");
      renderFilters(state.meta);
      await loadSkeleton();
    } catch (err) {
      document.querySelector("main").prepend(
        el("div", { class: "notice notice--error", text: "Could not load metadata: " + err.message })
      );
    }

    var fromHash = readHash();
    var cy = currentYear();
    state.expanded = fromHash || (cy === null ? new Set() : new Set([cy]));

    await load();
  }

  /* Never fail silently: any unexpected error becomes a visible banner. */
  init().catch(function (err) {
    var banner = document.createElement("div");
    banner.className = "notice notice--error";
    banner.style.margin = "20px";
    banner.textContent = "The timeline failed to start: " + (err && err.message || err);
    (document.querySelector("main") || document.body).prepend(banner);
    if (window.console) window.console.error(err);
  });
})();
