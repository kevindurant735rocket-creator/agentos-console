() => {
  const b = document.getElementById('promo');
  const ct = document.getElementById('promoct');
  const rows = b ? b.querySelectorAll('.e1').length : 0;
  const labelled = !!(b && b.getAttribute('aria-labelledby') === 'promoh');
  const all = (ct ? ct.textContent : '') + (b ? b.textContent : '');
  const overflow = b ? (b.scrollWidth > b.clientWidth + 2) : true;
  return {
    rows: rows,
    ct: ct ? ct.textContent : '',
    text: b ? b.textContent.slice(0, 160) : '',
    labelled: labelled,
    hasH: !!document.getElementById('promoh'),
    loading: /正在读/.test(all),
    unavailable: /不可用/.test(b ? b.textContent : ''),
    marks: b ? b.querySelectorAll('.e1 .k').length : 0,
    overflow: overflow
  };
}
