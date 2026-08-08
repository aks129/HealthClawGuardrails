/* healthclaw.io landing page behaviour.
 *
 * Lifted out of an inline <script> in templates/index.html. Inline blocks are
 * why the CSP still carries script-src 'unsafe-inline' (app.py notes it as
 * debt); every one moved to a file is one fewer reason to keep it.
 *
 * Three things happen here and nothing else: sections announce arrival, the
 * spec numbers count up once, and two forms do their work. No scroll-jacking,
 * no parallax — native scroll is what a phone gives us and what an in-app
 * webview reliably supports.
 */

(function () {
  'use strict';

  var STILL = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ── Sections announce arrival ──────────────────────────────────────── */
  var rising = document.querySelectorAll('.rise');

  if (STILL || !('IntersectionObserver' in window)) {
    // No observer, or the reader asked for stillness: show everything at
    // once. The failure mode to avoid is content that never becomes visible
    // because the mechanism that reveals it is missing.
    rising.forEach(function (el) { el.classList.add('in'); });
  } else {
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        seen.unobserve(e.target);
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    rising.forEach(function (el) { seen.observe(el); });
  }

  /* ── Spec numbers ───────────────────────────────────────────────────── */
  var numbers = document.querySelectorAll('.spec__num');

  function settle(el) {
    el.textContent = el.dataset.target;
  }

  function countTo(el, target, ms) {
    var t0 = performance.now();
    function tick(now) {
      var p = Math.min((now - t0) / ms, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = String(Math.round(target * eased));
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  if (STILL || !('IntersectionObserver' in window)) {
    numbers.forEach(settle);
  } else {
    var counting = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var target = parseInt(e.target.dataset.target, 10);
        if (isNaN(target)) { counting.unobserve(e.target); return; }
        countTo(e.target, target, 900);
        counting.unobserve(e.target);
      });
    }, { threshold: 0.5 });
    numbers.forEach(function (el) { counting.observe(el); });
  }

  /* ── Copy the quickstart ────────────────────────────────────────────── */
  var copyBtn = document.getElementById('copy-btn');
  var codeEl = document.getElementById('quickstart-code');

  if (copyBtn && codeEl) {
    copyBtn.addEventListener('click', function () {
      // Read the block that is actually on screen rather than a second copy
      // of the commands kept in this file. The old version hardcoded them,
      // so editing the template silently changed what you saw and not what
      // you copied.
      var text = codeEl.innerText.replace(/\n{3,}/g, '\n\n').trim();
      var done = function (ok) {
        copyBtn.textContent = ok ? 'Copied' : 'Press ⌘C';
        setTimeout(function () { copyBtn.textContent = 'Copy'; }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { done(true); },
          function () { done(false); }
        );
      } else {
        done(false);
      }
    });
  }

  /* ── Subscribe ──────────────────────────────────────────────────────── */
  var form = document.getElementById('subscribe-form');
  if (!form) return;

  var input = document.getElementById('subscribe-input');
  var btn = document.getElementById('subscribe-btn');
  var msg = document.getElementById('subscribe-msg');

  function say(text, kind) {
    msg.textContent = text;
    msg.className = 'form-msg' + (kind ? ' ' + kind : '');
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = input.value.trim();
    say('');

    if (!email) {
      say('Please enter your email.', 'error');
      input.focus();
      return;
    }

    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = 'Sending…';

    fetch('/api/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; })
          .then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (res) {
        if (res.ok) {
          say(res.data.already_subscribed
            ? 'You are already on the list — thanks.'
            : 'Subscribed. Watch your inbox.', 'ok');
          form.reset();
        } else {
          say(res.data.error || 'Something went wrong. Please try again.', 'error');
        }
      })
      .catch(function () {
        say('Network error. Please try again.', 'error');
      })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = original;
      });
  });
})();
