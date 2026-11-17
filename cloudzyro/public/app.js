/* رابط وب NightNote — Vanilla JS، بدون build و بدون CDN.
   آدرس API به‌ترتیب از این‌جاها خونده می‌شه:
   ۱) پارامتر ?api= در URL (و تو localStorage ذخیره می‌شه)
   ۲) window.__NN_API_BASE__ از config.js
   ۳) مقدار ذخیره‌شده تو localStorage (فیلد تنظیمات صفحه)
   ۴) خالی = relative (same-origin، حالت معمول وقتی FastAPI خودش رابط رو سرو می‌کنه) */
(function () {
  "use strict";

  var qs = new URLSearchParams(window.location.search);
  var apiParam = qs.get("api");
  if (apiParam) {
    try { localStorage.setItem("nn_api_base", apiParam); } catch (e) {}
  }
  function apiBase() {
    var stored = "";
    try { stored = localStorage.getItem("nn_api_base") || ""; } catch (e) {}
    var base = apiParam || (window.__NN_API_BASE__ || "") || stored || "";
    return String(base).replace(/\/+$/, "");
  }
  function apiUrl(path) {
    var base = apiBase();
    return base ? base + path : path;
  }
  function absUrl(path) {
    // لینک‌های /dl/ باید با همون base ساخته بشن (حالت cross-origin)
    if (/^https?:\/\//.test(path)) return path;
    return apiUrl(path);
  }

  var $ = function (id) { return document.getElementById(id); };
  var settings = {
    spoiler_effect: false,
    upload_as_document: false,
    timezone: null,
    show_rename_option: false,
    receive_screenshots: true
  };
  var currentInspect = null;
  var selectedQuality = "best";
  var currentJobId = null;
  var pollTimer = null;
  var confirmLarge = false;

  function showError(msg) {
    var el = $("global-error");
    el.textContent = msg;
    el.classList.remove("hidden");
  }
  function clearError() {
    $("global-error").classList.add("hidden");
    $("global-error").textContent = "";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtDuration(sec) {
    if (sec == null) return "";
    sec = Math.round(sec);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h > 0) return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
    return m + ":" + String(s).padStart(2, "0");
  }
  function fmtDate(iso) {
    if (!iso) return "";
    try {
      var opts = { dateStyle: "short", timeStyle: "short" };
      if (settings.timezone) opts.timeZone = settings.timezone;
      return new Intl.DateTimeFormat("fa-IR", opts).format(new Date(iso));
    } catch (e) {
      return new Date(iso).toLocaleString("fa-IR");
    }
  }
  async function apiFetch(path, opts) {
    opts = opts || {};
    opts.credentials = "include";
    opts.headers = opts.headers || {};
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    var resp = await fetch(apiUrl(path), opts);
    var data = null;
    var text = await resp.text();
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
    if (!resp.ok) {
      var detail = data && data.detail;
      var msg = typeof detail === "string" ? detail : (detail && detail.message) || ("خطا: " + resp.status);
      var err = new Error(msg);
      err.status = resp.status;
      err.detail = detail;
      throw err;
    }
    return data;
  }

  /* ---------------- تنظیمات ---------------- */
  async function loadSettings() {
    try {
      var s = await apiFetch("/api/settings");
      settings = s;
      $("set-spoiler").checked = !!s.spoiler_effect;
      $("set-document").checked = !!s.upload_as_document;
      $("set-rename").checked = !!s.show_rename_option;
      $("set-screenshots").checked = s.receive_screenshots !== false;
      $("set-timezone").value = s.timezone || "";
      applyRenameVisibility();
    } catch (e) { /* تنظیمات اختیاریه؛ اگه نشد، پیش‌فرض‌ها می‌مونن */ }
  }
  function applyRenameVisibility() {
    if (settings.show_rename_option) $("rename-row").classList.remove("hidden");
  }
  $("settings-save").addEventListener("click", async function () {
    clearError();
    var tz = $("set-timezone").value.trim();
    var apiBaseVal = $("set-api-base").value.trim();
    try { localStorage.setItem("nn_api_base", apiBaseVal); } catch (e) {}
    updateApiNote();
    try {
      var s = await apiFetch("/api/settings", {
        method: "PUT",
        body: {
          spoiler_effect: $("set-spoiler").checked,
          upload_as_document: $("set-document").checked,
          show_rename_option: $("set-rename").checked,
          receive_screenshots: $("set-screenshots").checked,
          timezone: tz || null
        }
      });
      settings = s;
      applyRenameVisibility();
      $("settings-msg").textContent = "✅ ذخیره شد.";
      loadHistory();
    } catch (e) {
      showError(e.message);
    }
  });
  function updateApiNote() {
    var base = apiBase();
    $("api-base-note").textContent = base ? ("API: " + base) : "API: همین آدرس (same-origin)";
    $("set-api-base").value = base;
  }

  /* ---------------- بررسی لینک ---------------- */
  function renderInspect(info) {
    currentInspect = info;
    $("inspect-card").classList.remove("hidden");
    var thumb = $("inspect-thumb");
    if (info.thumbnail_url && settings.receive_screenshots !== false) {
      thumb.src = info.thumbnail_url;
      thumb.classList.remove("hidden");
      thumb.classList.toggle("blurred", !!settings.spoiler_effect);
    } else {
      thumb.classList.add("hidden");
    }
    $("inspect-site").textContent = info.display_name + (info.is_playlist ? " — پلی‌لیست/آلبوم" : "");
    $("inspect-title").textContent = info.title || "بدون عنوان";
    var meta = [];
    if (info.artist) meta.push("👤 " + info.artist);
    if (info.album) meta.push("💿 " + info.album);
    if (info.duration) meta.push("⏱ " + fmtDuration(info.duration));
    $("inspect-meta").textContent = meta.join("  ");
    $("inspect-desc").textContent = info.description || "";
    var qRow = $("quality-row");
    qRow.innerHTML = "";
    qRow.classList.remove("hidden");
    selectedQuality = (info.qualities[0] && info.qualities[0].code) || "best";
    info.qualities.forEach(function (q) {
      var btn = document.createElement("button");
      btn.className = "btn quality-btn" + (q.code === selectedQuality ? " active" : "");
      btn.textContent = q.label;
      btn.addEventListener("click", function () {
        selectedQuality = q.code;
        qRow.querySelectorAll(".quality-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
      });
      qRow.appendChild(btn);
    });
    var warn = $("size-warning");
    if (info.size_warning) {
      warn.textContent = "⚠️ این فایل حدود " + Math.round(info.size_mb) + " مگابایته و دانلودش ممکنه طول بکشه. با زدن «دانلود»، تایید می‌کنی که ادامه بده.";
      warn.classList.remove("hidden");
      confirmLarge = true;
    } else {
      warn.classList.add("hidden");
      confirmLarge = false;
    }
    $("download-btn").classList.remove("hidden");
    $("retry-btn").classList.add("hidden");
    $("result-wrap").classList.add("hidden");
    $("playlist-wrap").classList.add("hidden");
    $("progress-wrap").classList.add("hidden");
    applyRenameVisibility();
  }

  $("inspect-btn").addEventListener("click", async function () {
    clearError();
    var url = $("url-input").value.trim();
    if (!url) { showError("اول یه لینک بفرست."); return; }
    var btn = $("inspect-btn");
    btn.disabled = true;
    btn.textContent = "در حال بررسی...";
    try {
      var info = await apiFetch("/api/inspect", { method: "POST", body: { url: url } });
      renderInspect(info);
    } catch (e) {
      showError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "بررسی لینک";
    }
  });
  $("url-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); $("inspect-btn").click(); }
  });

  /* ---------------- دانلود + polling ---------------- */
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function renderJob(job) {
    var p = job.progress || {};
    $("progress-wrap").classList.remove("hidden");
    var stageFa = {
      queued: "⏳ در صف دانلود...", downloading: "⬇️ در حال دانلود...",
      merging: "🔧 در حال ادغام ویدیو و صدا...", converting: "🎧 در حال تبدیل...",
      retrying: "🔁 تلاش دوباره...", finishing: "📦 در حال آماده‌سازی لینک...",
      done: "✅ دانلود کامل شد.", failed: "❌ دانلود ناموفق بود.",
      cancelled: "🚫 لغو شد.", playlist: "🎧 پلی‌لیست پیدا شد."
    };
    $("progress-stage").textContent = stageFa[p.stage] || job.status || "";
    $("progress-note").textContent = p.note || "";
    var bar = $("progress-bar");
    if (p.percent != null) {
      bar.classList.remove("indeterminate");
      bar.style.width = Math.max(0, Math.min(100, p.percent)) + "%";
    } else if (job.status === "downloading" || job.status === "pending" || job.status === "detecting") {
      bar.classList.add("indeterminate");
    }
    $("queue-info").textContent = p.queue_position
      ? ("🔢 جایگاه تو صف: " + p.queue_position + " (در حال اجرا/در صف: " + (p.active_count || "") + ")")
      : "";
    if (job.status === "downloading" || job.status === "pending" || job.status === "detecting" || job.status === "uploading") {
      $("cancel-btn").classList.remove("hidden");
      $("retry-btn").classList.add("hidden");
    } else {
      $("cancel-btn").classList.add("hidden");
    }
    if (job.status === "done" && job.download_url) {
      stopPolling();
      var wrap = $("result-wrap");
      wrap.classList.remove("hidden");
      var link = $("download-link");
      link.href = absUrl(job.download_url);
      link.setAttribute("download", job.file_name || "");
      $("result-meta").textContent = (job.file_name || "") + (job.file_size_mb ? (" — " + job.file_size_mb + " MB") : "");
      var preview = $("preview-wrap");
      preview.innerHTML = "";
      if (!settings.upload_as_document && job.file_type) {
        var previewUrl = absUrl(job.download_url) + (job.download_url.indexOf("?") >= 0 ? "&" : "?") + "preview=1";
        if (job.file_type === "video" || job.file_type === "video_note") {
          var v = document.createElement("video");
          v.controls = true; v.src = previewUrl; v.style.maxWidth = "100%";
          preview.appendChild(v);
        } else if (job.file_type === "audio") {
          var a = document.createElement("audio");
          a.controls = true; a.src = previewUrl;
          preview.appendChild(a);
        } else if (job.file_type === "photo") {
          var img = document.createElement("img");
          img.src = previewUrl; img.style.maxWidth = "100%";
          if (settings.spoiler_effect) img.classList.add("blurred");
          preview.appendChild(img);
        }
      }
      loadHistory();
    }
    if (job.status === "failed" || job.status === "cancelled") {
      stopPolling();
      $("retry-btn").classList.remove("hidden");
      if (job.error) showError(job.error);
      loadHistory();
    }
    if (job.children && job.children.length) {
      var pw = $("playlist-wrap");
      pw.classList.remove("hidden");
      var list = $("playlist-list");
      list.innerHTML = "";
      job.children.forEach(function (child, i) {
        var li = document.createElement("li");
        var label = (child.playlist_index || (i + 1)) + ". " + (child.title || child.url);
        var statusFa = { done: "✅", failed: "❌", cancelled: "🚫", downloading: "⬇️", pending: "⏳", detecting: "⏳", uploading: "⬆️" }[child.status] || child.status;
        li.innerHTML = esc(statusFa + " " + label);
        if (child.status === "done" && child.download_url) {
          var a = document.createElement("a");
          a.className = "btn"; a.href = absUrl(child.download_url); a.textContent = "⬇️ دانلود";
          a.setAttribute("download", child.file_name || "");
          li.appendChild(document.createTextNode(" "));
          li.appendChild(a);
        }
        if (child.error) {
          var em = document.createElement("div");
          em.className = "muted"; em.textContent = child.error;
          li.appendChild(em);
        }
        list.appendChild(li);
      });
      var pp = job.playlist_progress;
      if (pp && pp.done + pp.failed < pp.total) {
        // هنوز فرزندها در حال دانلودن — polling ادامه پیدا می‌کنه
      } else if (pp) {
        stopPolling();
      }
    }
  }
  async function pollJob() {
    if (!currentJobId) return;
    try {
      var job = await apiFetch("/api/jobs/" + currentJobId);
      renderJob(job);
    } catch (e) {
      stopPolling();
      showError(e.message);
    }
  }
  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollJob, 2000);
  }

  $("download-btn").addEventListener("click", async function () {
    clearError();
    var url = $("url-input").value.trim();
    if (!url) return;
    var rename = $("rename-input").value.trim();
    var btn = $("download-btn");
    btn.disabled = true;
    try {
      var job = await apiFetch("/api/jobs", {
        method: "POST",
        body: { url: url, quality: selectedQuality, custom_filename: rename || null, confirm_large: confirmLarge }
      });
      currentJobId = job.id;
      renderJob(job);
      if (job.status !== "done" && job.status !== "failed") startPolling();
    } catch (e) {
      if (e.status === 409 && e.detail && e.detail.requires_confirmation) {
        // هشدار حجم از سمت سرور: با تایید دوباره بفرست
        confirmLarge = true;
        try {
          var job2 = await apiFetch("/api/jobs", {
            method: "POST",
            body: { url: url, quality: selectedQuality, custom_filename: rename || null, confirm_large: true }
          });
          currentJobId = job2.id;
          renderJob(job2);
          if (job2.status !== "done" && job2.status !== "failed") startPolling();
        } catch (e2) { showError(e2.message); }
      } else {
        showError(e.message);
      }
    } finally {
      btn.disabled = false;
    }
  });
  $("cancel-btn").addEventListener("click", async function () {
    if (!currentJobId) return;
    try {
      var job = await apiFetch("/api/jobs/" + currentJobId + "/cancel", { method: "POST" });
      stopPolling();
      renderJob(job);
    } catch (e) { showError(e.message); }
  });
  $("retry-btn").addEventListener("click", async function () {
    if (!currentJobId) return;
    clearError();
    try {
      var job = await apiFetch("/api/jobs/" + currentJobId + "/retry", { method: "POST" });
      renderJob(job);
      startPolling();
    } catch (e) { showError(e.message); }
  });

  /* ---------------- آپلود ---------------- */
  $("upload-btn").addEventListener("click", async function () {
    clearError();
    var input = $("upload-input");
    if (!input.files || !input.files[0]) { showError("اول یه فایل انتخاب کن."); return; }
    var fd = new FormData();
    fd.append("file", input.files[0]);
    var btn = $("upload-btn");
    btn.disabled = true;
    btn.textContent = "در حال آپلود...";
    try {
      var job = await apiFetch("/api/upload", { method: "POST", body: fd });
      $("upload-result").classList.remove("hidden");
      var link = $("upload-link");
      link.href = absUrl(job.download_url);
      link.setAttribute("download", job.file_name || "");
      $("upload-meta").textContent = (job.file_name || "") + (job.file_size_mb ? (" — " + job.file_size_mb + " MB") : "");
      loadHistory();
    } catch (e) {
      showError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "آپلود و گرفتن لینک";
    }
  });

  /* ---------------- تاریخچه ---------------- */
  async function loadHistory() {
    var list = $("history-list");
    try {
      var data = await apiFetch("/api/history?limit=20");
      list.innerHTML = "";
      if (!data.jobs || !data.jobs.length) {
        list.innerHTML = "<li class='muted'>هنوز دانلودی نداری.</li>";
        return;
      }
      data.jobs.forEach(function (job) {
        var li = document.createElement("li");
        var statusFa = { done: "✅", failed: "❌", cancelled: "🚫", downloading: "⬇️", pending: "⏳", detecting: "⏳", uploading: "⬆️" }[job.status] || job.status;
        var title = job.title || job.url;
        li.innerHTML = "<b>" + esc(statusFa + " " + title) + "</b><br><span class='muted'>" +
          esc(job.display_name || job.site || "") + " — " + esc(fmtDate(job.created_at)) +
          (job.file_size_mb ? " — " + job.file_size_mb + " MB" : "") + "</span>";
        if (job.status === "done" && job.download_url) {
          var a = document.createElement("a");
          a.className = "btn"; a.href = absUrl(job.download_url); a.textContent = "⬇️ دانلود";
          a.setAttribute("download", job.file_name || "");
          li.appendChild(document.createTextNode(" "));
          li.appendChild(a);
        }
        if (job.error) {
          var em = document.createElement("div");
          em.className = "muted"; em.textContent = job.error;
          li.appendChild(em);
        }
        list.appendChild(li);
      });
    } catch (e) {
      list.innerHTML = "<li class='muted'>تاریخچه در دسترس نیست.</li>";
    }
  }
  $("history-refresh").addEventListener("click", loadHistory);

  /* ---------------- پنل ادمین ---------------- */
  async function checkAdmin() {
    try {
      await apiFetch("/api/admin/me");
      $("admin-login").classList.add("hidden");
      $("admin-panel").classList.remove("hidden");
      loadAdminData();
    } catch (e) {
      $("admin-login").classList.remove("hidden");
      $("admin-panel").classList.add("hidden");
    }
  }
  $("admin-login-btn").addEventListener("click", async function () {
    var errEl = $("admin-login-error");
    errEl.classList.add("hidden");
    try {
      await apiFetch("/api/admin/login", { method: "POST", body: { password: $("admin-password").value } });
      $("admin-password").value = "";
      checkAdmin();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.classList.remove("hidden");
    }
  });
  $("admin-logout-btn").addEventListener("click", async function () {
    try { await apiFetch("/api/admin/logout", { method: "POST" }); } catch (e) {}
    checkAdmin();
  });
  async function loadAdminData() {
    try {
      var st = await apiFetch("/api/admin/stats");
      $("admin-stats").innerHTML =
        "<div>وضعیت<b>" + (st.bot_enabled ? "🟢 روشن" : "🔴 خاموش") + "</b></div>" +
        "<div>دانلودهای امروز<b>" + st.today.total_downloads + "</b></div>" +
        "<div>موفق<b>" + st.today.successful_downloads + "</b></div>" +
        "<div>ناموفق<b>" + st.today.failed_downloads + "</b></div>" +
        "<div>حجم امروز<b>" + st.today.total_size_mb + " MB</b></div>" +
        "<div>جاب فعال<b>" + st.active_jobs + "</b></div>" +
        "<div>کاربران<b>" + st.total_users + " (" + st.web_users + " وب)</b></div>" +
        "<div>پرکاربردترین<b>" + esc(st.today.most_used_site || "-") + "</b></div>";
      $("admin-toggle").checked = !!st.bot_enabled;
      $("admin-maxsize").value = st.max_file_size_mb;
    } catch (e) { showError(e.message); }
    try {
      var ts = await apiFetch("/api/admin/topsites");
      var ul = $("admin-topsites");
      ul.innerHTML = "";
      ts.sites.forEach(function (s) {
        var li = document.createElement("li");
        li.textContent = s.site + " — " + s.count;
        ul.appendChild(li);
      });
      if (ts.unknown_domains && ts.unknown_domains.length) {
        var h = document.createElement("li");
        h.className = "muted";
        h.textContent = "دامنه‌های ناشناس: " + ts.unknown_domains.map(function (d) { return d.domain + " (" + d.count + ")"; }).join("، ");
        ul.appendChild(h);
      }
    } catch (e) {}
    try {
      var texts = await apiFetch("/api/admin/texts");
      var wrap = $("admin-texts");
      wrap.innerHTML = "";
      texts.texts.forEach(function (t) {
        var div = document.createElement("div");
        div.className = "row";
        var label = document.createElement("label");
        label.textContent = t.label + (t.customized ? " (تغییر داده شده)" : "");
        label.style.minWidth = "220px";
        var ta = document.createElement("textarea");
        ta.rows = 2; ta.value = t.current;
        var save = document.createElement("button");
        save.className = "btn"; save.textContent = "ذخیره";
        save.addEventListener("click", async function () {
          try {
            await apiFetch("/api/admin/texts", { method: "POST", body: { key: t.key, value: ta.value } });
            save.textContent = "✅ ذخیره شد";
            setTimeout(function () { save.textContent = "ذخیره"; }, 1500);
          } catch (e2) { showError(e2.message); }
        });
        div.appendChild(label); div.appendChild(ta); div.appendChild(save);
        if (t.hint) {
          var hint = document.createElement("div");
          hint.className = "muted"; hint.textContent = t.hint;
          div.appendChild(hint);
        }
        wrap.appendChild(div);
      });
    } catch (e) {}
  }
  $("admin-refresh-btn").addEventListener("click", loadAdminData);
  $("admin-toggle-save").addEventListener("click", async function () {
    try {
      await apiFetch("/api/admin/toggle", { method: "POST", body: { enabled: $("admin-toggle").checked } });
      loadAdminData();
    } catch (e) { showError(e.message); }
  });
  $("admin-maxsize-save").addEventListener("click", async function () {
    try {
      await apiFetch("/api/admin/maxsize", { method: "POST", body: { mb: parseInt($("admin-maxsize").value, 10) } });
      loadAdminData();
    } catch (e) { showError(e.message); }
  });
  $("admin-user-search").addEventListener("click", async function () {
    var q = $("admin-user-q").value.trim();
    try {
      var data = await apiFetch("/api/admin/users?q=" + encodeURIComponent(q));
      var ul = $("admin-users");
      ul.innerHTML = "";
      if (!data.users.length) ul.innerHTML = "<li class='muted'>کاربری پیدا نشد.</li>";
      data.users.forEach(function (u) {
        var li = document.createElement("li");
        li.innerHTML = "<b>" + esc(u.full_name || "") + "</b> " + esc(u.username ? ("@" + u.username) : "") +
          " <span class='muted'>(" + u.telegram_id + (u.is_web ? "، وب" : "، تلگرام") + ")</span><br>" +
          "<span class='muted'>کل: " + u.summary.total + "، موفق: " + u.summary.success + "، امروز: " + u.summary.today +
          (u.is_banned ? " — 🚫 بن شده" + (u.ban_reason ? (": " + esc(u.ban_reason)) : "") : "") + "</span>";
        var btn = document.createElement("button");
        btn.className = "btn " + (u.is_banned ? "" : "danger");
        btn.textContent = u.is_banned ? "✅ رفع بن" : "🚫 بن";
        btn.addEventListener("click", async function () {
          var path = "/api/admin/users/" + u.telegram_id + (u.is_banned ? "/unban" : "/ban");
          try {
            await apiFetch(path, { method: "POST", body: u.is_banned ? {} : { reason: null } });
            $("admin-user-search").click();
          } catch (e) { showError(e.message); }
        });
        li.appendChild(document.createTextNode(" "));
        li.appendChild(btn);
        ul.appendChild(li);
      });
    } catch (e) { showError(e.message); }
  });
  $("admin-broadcast-btn").addEventListener("click", async function () {
    var text = $("admin-broadcast-text").value.trim();
    if (!text) { showError("متن پیام همگانی رو بنویس."); return; }
    try {
      var bc = await apiFetch("/api/admin/broadcast", {
        method: "POST",
        body: { text: text, target: $("admin-broadcast-target").value }
      });
      var statusEl = $("admin-broadcast-status");
      statusEl.textContent = "📤 در حال ارسال...";
      var timer = setInterval(async function () {
        try {
          var st = await apiFetch("/api/admin/broadcast/" + bc.id);
          statusEl.textContent = "📤 " + st.sent + " موفق، " + st.failed + " ناموفق از " + st.total + " (" + st.state + ")";
          if (st.state === "done" || st.state === "failed") clearInterval(timer);
        } catch (e) { clearInterval(timer); }
      }, 2000);
    } catch (e) { showError(e.message); }
  });

  /* ---------------- شروع ---------------- */
  updateApiNote();
  loadSettings();
  loadHistory();
  checkAdmin();
})();
