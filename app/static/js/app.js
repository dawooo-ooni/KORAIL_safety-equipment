/* 공용 스크립트: 확인창, 플래시 닫기, 필터 자동 제출, 행 클릭, 등록 미리보기, 점검·라벨 선택 */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // data-confirm: 제출/클릭 전 확인
  document.addEventListener('submit', function (e) {
    var f = e.target;
    var msg = f.getAttribute('data-confirm') || (e.submitter && e.submitter.getAttribute('data-confirm'));
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-confirm]');
    if (a && !window.confirm(a.getAttribute('data-confirm'))) e.preventDefault();
  });

  // 플래시 메시지: 닫기 버튼, 일반 메시지는 6초 후 사라짐 (임시 비밀번호는 유지)
  $$('.flash').forEach(function (el) {
    var b = el.querySelector('button');
    if (b) b.addEventListener('click', function () { el.remove(); });
    if (!el.classList.contains('temp') && !el.classList.contains('error')) {
      setTimeout(function () { el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 400); }, 6000);
    }
  });

  // select[data-autosubmit]: 값 바꾸면 폼 제출
  $$('select[data-autosubmit]').forEach(function (s) {
    s.addEventListener('change', function () { s.form.submit(); });
  });

  // tr[data-href]: 행 클릭 시 이동 (버튼·링크 클릭은 제외)
  $$('tr[data-href]').forEach(function (tr) {
    tr.classList.add('link');
    tr.addEventListener('click', function (e) {
      if (e.target.closest('a,button,input,select,label,form')) return;
      window.location = tr.getAttribute('data-href');
    });
  });

  // 장비 등록/수정: 자동 계산 미리보기
  var pv = $('#calcPreview');
  if (pv) {
    var url = pv.getAttribute('data-url');
    var ids = ['type_id', 'last_inspect_date', 'issue_date', 'last_result', 'status'];
    var upd = function () {
      var v = {}; ids.forEach(function (i) { var el = document.getElementById(i); v[i] = el ? el.value : ''; });
      if (!v.type_id || !v.last_inspect_date) {
        pv.innerHTML = '<span class="muted">장비구분과 최종검사일을 입력하면 자동 계산 결과가 표시됩니다.</span>';
        return;
      }
      var q = '?type_id=' + encodeURIComponent(v.type_id) + '&last=' + encodeURIComponent(v.last_inspect_date) +
        '&issue=' + encodeURIComponent(v.issue_date) + '&result=' + encodeURIComponent(v.last_result) +
        '&status=' + encodeURIComponent(v.status);
      fetch(url + q, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok) { pv.textContent = ''; return; }
        pv.innerHTML = '';
        var box = document.createElement('div'); box.className = 'info'; box.style.margin = '0';
        box.appendChild(document.createTextNode('자동 계산 → 차기검사일 '));
        var b = document.createElement('b'); b.textContent = d.next; box.appendChild(b);
        box.appendChild(document.createTextNode(' · 남은 일수 '));
        var dd = document.createElement('span'); dd.className = 'dd ' + d.css; dd.textContent = d.dd; box.appendChild(dd);
        box.appendChild(document.createTextNode(' · '));
        var bg = document.createElement('span'); bg.className = 'badge g-' + d.css; bg.textContent = d.icon + ' ' + d.label; box.appendChild(bg);
        if (d.repl) box.appendChild(document.createTextNode(' · 교체예정일 ' + d.repl));
        if (d.reason) box.appendChild(document.createTextNode(' · ' + d.reason));
        pv.appendChild(box);
      }).catch(function () { pv.textContent = ''; });
    };
    ids.forEach(function (i) { var el = document.getElementById(i); if (el) el.addEventListener('change', upd); });
    upd();
  }

  // 목록 선택 필터 (작업 전 점검 · QR 라벨)
  $$('input[data-filter-list]').forEach(function (inp) {
    var list = document.getElementById(inp.getAttribute('data-filter-list'));
    inp.addEventListener('input', function () {
      var q = inp.value.trim().toLowerCase();
      $$('label', list).forEach(function (l) {
        l.classList.toggle('hide', q && l.textContent.toLowerCase().indexOf(q) < 0);
      });
    });
  });

  // 작업 전 점검: 체크박스 ↔ 입력칸 동기화
  var ckText = $('#nos');
  if (ckText) {
    var parse = function () {
      return ckText.value.split(/[\s,;]+/).map(function (s) { return s.trim().toUpperCase(); }).filter(Boolean);
    };
    var cnt = $('#pickCount');
    var sync = function () {
      var set = parse();
      $$('input[name=pickui]').forEach(function (c) { c.checked = set.indexOf(c.value) >= 0; });
      if (cnt) cnt.textContent = set.length;
    };
    $$('input[name=pickui]').forEach(function (c) {
      c.addEventListener('change', function () {
        var set = parse();
        if (c.checked && set.indexOf(c.value) < 0) set.push(c.value);
        if (!c.checked) set = set.filter(function (x) { return x !== c.value; });
        ckText.value = set.join(', ');
        if (cnt) cnt.textContent = set.length;
      });
    });
    ckText.addEventListener('input', sync);
    var clr = $('#ckClear');
    if (clr) clr.addEventListener('click', function () { ckText.value = ''; sync(); });
    sync();
  }

  // QR 라벨: 전체 선택/해제, 선택 수
  var lbForm = $('#labelForm');
  if (lbForm) {
    var lbCount = $('#labelCount');
    var btn = $('#labelPrint');
    var refresh = function () {
      var n = $$('input[name=ids]:checked', lbForm).length;
      lbCount.textContent = n;
      btn.disabled = !n;
    };
    lbForm.addEventListener('change', refresh);
    $$('[data-select]').forEach(function (b) {
      b.addEventListener('click', function () {
        var on = b.getAttribute('data-select') === 'all';
        $$('label:not(.hide) input[name=ids]', lbForm).forEach(function (c) { c.checked = on; });
        refresh();
      });
    });
    refresh();
  }

  // 알림: 전체 선택
  var allRec = $('#recAll');
  if (allRec) allRec.addEventListener('change', function () {
    $$('input[name=recipient]').forEach(function (c) { c.checked = allRec.checked; });
  });

  // 인쇄 페이지 자동 인쇄
  if (document.body.hasAttribute('data-autoprint')) {
    window.addEventListener('load', function () { setTimeout(function () { window.print(); }, 400); });
  }
  $$('[data-print]').forEach(function (b) { b.addEventListener('click', function () { window.print(); }); });

  // 사진 파일 선택하면 바로 업로드
  $$('input[data-autoupload]').forEach(function (inp) {
    inp.addEventListener('change', function () { if (inp.files.length) inp.form.submit(); });
  });
})();
