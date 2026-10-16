/* Northfeed floating hearts, same routine as the website's fx.js */
(() => { if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const cv = document.createElement('canvas'); cv.id = 'hearts'; cv.setAttribute('aria-hidden', 'true'); cv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0'; document.body.prepend(cv);
  const c = cv.getContext('2d'); let W, H; const fit = () => { W = cv.width = innerWidth; H = cv.height = innerHeight; }; fit(); addEventListener('resize', fit);
  const N = innerWidth < 700 ? 12 : 22;
  const hs = Array.from({ length: N }, () => ({ x: Math.random() * W, y: Math.random() * H, s: 7 + Math.random() * 16, v: .12 + Math.random() * .3, a: .07 + Math.random() * .12, p: Math.random() * Math.PI * 2 }));
  const heart = (x, y, s) => { c.beginPath(); c.moveTo(x, y + s * .3); c.bezierCurveTo(x, y, x - s * .5, y - s * .1, x - s * .5, y + s * .25); c.bezierCurveTo(x - s * .5, y + s * .55, x, y + s * .8, x, y + s); c.bezierCurveTo(x, y + s * .8, x + s * .5, y + s * .55, x + s * .5, y + s * .25); c.bezierCurveTo(x + s * .5, y - s * .1, x, y, x, y + s * .3); c.closePath(); };
  let t = 0; const tick = () => { t += .016; c.clearRect(0, 0, W, H); if (!document.hidden) hs.forEach(h => { h.y -= h.v; h.x += Math.sin(t + h.p) * .25; if (h.y < -40) { h.y = H + 40; h.x = Math.random() * W; } c.fillStyle = 'rgba(255,92,154,' + h.a + ')'; c.shadowColor = '#ff5c9a'; c.shadowBlur = 16; heart(h.x, h.y, h.s); c.fill(); }); c.shadowBlur = 0; requestAnimationFrame(tick); }; tick(); })();
