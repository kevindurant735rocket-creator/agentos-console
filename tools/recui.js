() => {
  const b = document.getElementById('recs');
  const ct = document.getElementById('recct');
  const rows = b ? b.querySelectorAll('.rc1').length : 0;
  const labelled = !!(b && b.getAttribute('aria-labelledby') === 'rech');
  const loading = /正在读/.test((ct ? ct.textContent : '') + (b ? b.textContent : ''));
  return {
    rows: rows,
    ct: ct ? ct.textContent : '',
    text: b ? b.textContent.slice(0, 120) : '',
    labelled: labelled,
    hasH: !!document.getElementById('rech'),
    loading: loading
  };
}
