'use strict';

const GITHUB_URL = "https://github.com/yrd-go/commercepilot-case-study";

function setupGithubLinks() {
  const valid = /^https:\/\/github\.com\/[^/]+\/[^/]+\/?$/.test(GITHUB_URL);
  document.querySelectorAll('[data-github]').forEach((link) => {
    if (valid) {
      link.href = GITHUB_URL;
      link.target = '_blank';
      link.rel = 'noopener';
      link.removeAttribute('aria-disabled');
      link.classList.remove('is-disabled');
      return;
    }
    link.href = '#';
    link.setAttribute('aria-disabled', 'true');
    link.classList.add('is-disabled');
    link.title = '作品集仓库地址将在发布后填入';
    link.addEventListener('click', (event) => event.preventDefault());
  });
}

function setupReveal() {
  document.documentElement.classList.add('js');
  const items = Array.from(document.querySelectorAll('.reveal'));
  if (!items.length) return;
  if (!('IntersectionObserver' in window)) {
    items.forEach((item) => item.classList.add('in'));
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('in');
      observer.unobserve(entry.target);
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
  items.forEach((item) => observer.observe(item));
}

function setupNav() {
  const links = Array.from(document.querySelectorAll('.site-nav a[href^="#"]'));
  const sections = links
    .map((link) => document.querySelector(link.getAttribute('href')))
    .filter(Boolean);
  if (!sections.length || !('IntersectionObserver' in window)) return;
  const observer = new IntersectionObserver((entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    links.forEach((link) => {
      const active = link.getAttribute('href') === `#${visible.target.id}`;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }, { rootMargin: '-20% 0px -58% 0px', threshold: [0.05, 0.25, 0.5] });
  sections.forEach((section) => observer.observe(section));
}

function setupLightbox() {
  const box = document.getElementById('lightbox');
  if (!box) return;
  const image = box.querySelector('img');
  const close = box.querySelector('.lightbox-close');
  const selectors = [
    '.case-media img',
    '.chart-card img',
    '.gallery-strip img',
    '.hero-visual img',
  ].join(',');
  const open = (src, alt) => {
    image.src = src;
    image.alt = alt || '';
    box.classList.add('open');
    box.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    close?.focus();
  };
  const hide = () => {
    box.classList.remove('open');
    box.setAttribute('aria-hidden', 'true');
    image.src = '';
    document.body.style.overflow = '';
  };
  document.querySelectorAll(selectors).forEach((img) => {
    img.classList.add('zoomable');
    img.setAttribute('tabindex', '0');
    img.addEventListener('click', () => open(img.currentSrc || img.src, img.alt));
    img.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open(img.currentSrc || img.src, img.alt);
      }
    });
  });
  close?.addEventListener('click', hide);
  box.addEventListener('click', (event) => {
    if (event.target === box) hide();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && box.classList.contains('open')) hide();
  });
}

setupGithubLinks();
setupReveal();
setupNav();
setupLightbox();
