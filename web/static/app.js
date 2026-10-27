/* EnglishFoundry · 原生 JS：客户端搜索与筛选、难度档位、练习判分、听力倍速。
   不依赖任何库，也不 fetch 任何文件（数据就在 DOM 上），所以 file:// 双击打开同样可用。 */
(function () {
  'use strict';

  var doc = document;
  function all(selector, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(selector)); }
  function one(selector, scope) { return (scope || doc).querySelector(selector); }
  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (error) { /* 隐私模式或 file:// 下取不到 storage：静默降级 */ }
    return null;
  }

  /* ---------------- 搜索 + 角色/类别筛选 ---------------- */
  var grid = one('[data-story-grid]');
  var searchInput = one('[data-search]');
  var clearButton = one('[data-clear]');
  var countNode = one('[data-result-count]');
  var emptyNode = one('[data-empty-state]');
  var groups = {};
  all('[data-filter-group]').forEach(function (group) {
    groups[group.getAttribute('data-filter-group')] = group;
  });

  var state = { q: '', role: 'all', category: 'all' };

  function readHash() {
    var raw = (window.location.hash || '').replace(/^#/, '');
    if (!raw || raw === 'library' || raw === 'roles' || raw === 'listening') return;
    raw.split('&').forEach(function (pair) {
      var parts = pair.split('=');
      var key = decodeURIComponent(parts[0] || '');
      var value = decodeURIComponent(parts.slice(1).join('=') || '');
      if (key === 'q') state.q = value;
      if (key === 'role') state.role = value || 'all';
      if (key === 'category') state.category = value || 'all';
    });
  }

  function writeHash() {
    var bits = [];
    if (state.role !== 'all') bits.push('role=' + encodeURIComponent(state.role));
    if (state.category !== 'all') bits.push('category=' + encodeURIComponent(state.category));
    if (state.q) bits.push('q=' + encodeURIComponent(state.q));
    var hash = bits.length ? '#' + bits.join('&') : '#library';
    try {
      window.history.replaceState(null, '', hash);
    } catch (error) { /* file:// 下 replaceState 可能被拒绝，忽略 */ }
  }

  function matches(card) {
    var roles = (card.getAttribute('data-roles') || '').split(/\s+/);
    if (state.role !== 'all' && roles.indexOf(state.role) === -1) return false;
    if (state.category !== 'all' && (card.getAttribute('data-category') || '') !== state.category) return false;
    if (!state.q) return true;
    var haystack = (card.getAttribute('data-search') || '').toLowerCase();
    return state.q.toLowerCase().split(/\s+/).filter(Boolean).every(function (token) {
      return haystack.indexOf(token) !== -1;
    });
  }

  function paintChips() {
    Object.keys(groups).forEach(function (name) {
      all('[data-filter-value]', groups[name]).forEach(function (chip) {
        var active = chip.getAttribute('data-filter-value') === state[name];
        chip.classList.toggle('is-active', active);
        chip.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    });
  }

  function apply(options) {
    if (!grid) return;
    var cards = all('.story-card', grid);
    var visible = 0;
    cards.forEach(function (card) {
      var ok = matches(card);
      card.classList.toggle('is-hidden', !ok);
      if (ok) visible += 1;
    });
    if (countNode) {
      var total = cards.length;
      countNode.textContent = visible === total
        ? total + ' stories 篇'
        : visible + ' / ' + total + ' stories 篇';
    }
    if (emptyNode) emptyNode.hidden = visible !== 0;
    if (clearButton) clearButton.hidden = !state.q;
    paintChips();
    if (!options || options.hash !== false) writeHash();
  }

  Object.keys(groups).forEach(function (name) {
    groups[name].addEventListener('click', function (event) {
      var chip = event.target.closest('[data-filter-value]');
      if (!chip) return;
      state[name] = chip.getAttribute('data-filter-value');
      apply();
    });
  });

  if (searchInput) {
    var timer = 0;
    searchInput.addEventListener('input', function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        state.q = searchInput.value.trim();
        apply();
      }, 120);
    });
    searchInput.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { searchInput.value = ''; state.q = ''; apply(); }
    });
  }

  all('[data-clear], [data-reset]').forEach(function (button) {
    button.addEventListener('click', function () {
      state = { q: '', role: 'all', category: 'all' };
      if (searchInput) searchInput.value = '';
      apply();
      if (searchInput) searchInput.focus();
    });
  });

  doc.addEventListener('keydown', function (event) {
    if (event.key !== '/' || !searchInput) return;
    var tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    searchInput.focus();
    searchInput.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });

  if (grid) {
    readHash();
    if (searchInput && state.q) searchInput.value = state.q;
    apply({ hash: false });
  }

  /* ---------------- 难度档位 tabs ---------------- */
  var tabs = all('.level-tab');
  if (tabs.length) {
    var panels = {};
    all('[data-level-panel]').forEach(function (panel) {
      panels[panel.getAttribute('data-level-panel')] = panel;
    });

    function showLevel(code, remember) {
      if (!panels[code]) return;
      Object.keys(panels).forEach(function (key) {
        panels[key].classList.toggle('is-active', key === code);
      });
      tabs.forEach(function (tab) {
        var active = tab.getAttribute('data-level') === code;
        tab.classList.toggle('is-active', active);
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      if (remember) store('foundry:level', code);
    }

    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () { showLevel(tab.getAttribute('data-level'), true); });
    });

    var wanted = (window.location.hash || '').match(/^#level-([A-Za-z0-9]+)/);
    var saved = store('foundry:level');
    if (wanted && panels[wanted[1].toUpperCase()]) {
      showLevel(wanted[1].toUpperCase(), false);
    } else if (saved && panels[saved]) {
      showLevel(saved, false);
    }
  }

  /* ---------------- 练习判分 ---------------- */
  all('[data-quiz]').forEach(function (quiz) {
    var questions = all('.quiz-q', quiz);
    var scoreNode = one('[data-quiz-score]', quiz);
    var checkButton = one('[data-quiz-check]', quiz);
    var resetButton = one('[data-quiz-reset]', quiz);

    function grade() {
      var answered = 0, correct = 0;
      questions.forEach(function (question) {
        var answer = (question.getAttribute('data-answer') || '').toUpperCase();
        var picked = one('input:checked', question);
        question.classList.remove('is-correct', 'is-wrong');
        all('.quiz-choice', question).forEach(function (choice) {
          choice.classList.remove('is-answer', 'is-picked-wrong');
        });
        var explain = one('.quiz-explain', question);
        if (explain) explain.hidden = true;
        if (!answer) return;
        var right = one('.quiz-choice input[value="' + answer + '"]', question);
        if (right && right.parentElement) right.parentElement.classList.add('is-answer');
        if (!picked) return;
        answered += 1;
        if (explain) explain.hidden = false;
        if (picked.value.toUpperCase() === answer) {
          correct += 1;
          question.classList.add('is-correct');
        } else {
          question.classList.add('is-wrong');
          if (picked.parentElement) picked.parentElement.classList.add('is-picked-wrong');
        }
      });
      if (scoreNode) {
        scoreNode.textContent = answered
          ? 'Score 得分：' + correct + ' / ' + answered + (answered === questions.length ? ' 🎉' : '（还有 ' + (questions.length - answered) + ' 题未答）')
          : 'Pick an answer first 先选答案再对。';
      }
    }

    function reset() {
      questions.forEach(function (question) {
        all('input', question).forEach(function (input) { input.checked = false; });
        question.classList.remove('is-correct', 'is-wrong');
        all('.quiz-choice', question).forEach(function (choice) {
          choice.classList.remove('is-answer', 'is-picked-wrong');
        });
        var explain = one('.quiz-explain', question);
        if (explain) explain.hidden = true;
      });
      if (scoreNode) scoreNode.textContent = '';
    }

    if (checkButton) checkButton.addEventListener('click', grade);
    if (resetButton) resetButton.addEventListener('click', reset);
  });

  /* ---------------- 听力倍速 ---------------- */
  var speed = Number(store('foundry:speed')) || 1;
  all('[data-speed-group]').forEach(function (group) {
    var card = group.closest('.player-card') || group.parentElement;
    var player = card ? one('audio', card) : null;
    if (!player) return;
    all('[data-speed]', group).forEach(function (button) {
      button.addEventListener('click', function () {
        var rate = Number(button.getAttribute('data-speed')) || 1;
        player.playbackRate = rate;
        store('foundry:speed', String(rate));
        all('[data-speed]', group).forEach(function (other) {
          var active = other === button;
          other.classList.toggle('is-active', active);
          other.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
      });
    });
    if (speed !== 1) {
      player.addEventListener('loadedmetadata', function () { player.playbackRate = speed; });
      all('[data-speed]', group).forEach(function (other) {
        var active = Number(other.getAttribute('data-speed')) === speed;
        other.classList.toggle('is-active', active);
        other.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    }
  });

  /* ---------------- 锚点滚动时补高亮（避免 sticky 头挡住标题） ---------------- */
  all('a[href*="#"]').forEach(function (link) {
    link.addEventListener('click', function () {
      var target = null;
      try { target = link.hash ? one(link.hash) : null; } catch (error) { target = null; }
      if (target) window.setTimeout(function () { target.scrollIntoView({ block: 'start', behavior: 'smooth' }); }, 60);
    });
  });
})();
