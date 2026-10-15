(() => {
  const b = document.getElementById('dist');
  const h = document.getElementById('disth');
  const ct = document.getElementById('distct');
  const t = b ? b.textContent : '';
  return {
    rows: b ? b.querySelectorAll('.e1').length : 0,
    loading: /正在读/.test(t),
    unavailable: /不可用/.test(t),
    labelled: !!(b && h && b.getAttribute('aria-labelledby') === h.id),
    hasH: !!h,
    ct: ct ? ct.textContent : '',
    overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    first: b && b.querySelector('.e1') ? b.querySelector('.e1').textContent.trim().slice(0, 80) : '',
  };
})()
