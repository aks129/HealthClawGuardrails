/**
 * Design invariants that only a browser can check.
 *
 * Everything else guarding the design system reads source: design.md says a
 * token, check_table_stakes.py greps an added line, a pytest asserts a class
 * exists. None of that can see CASCADE. The redesign shipped a preview where
 * the primary call to action rendered ultramarine text on a near-black button
 * and the whole nav turned blue, because the base rule was written
 * `.surface-paper a` — specificity (0,1,1) — and so beat every single-class
 * component below it. The source was correct in isolation; only the computed
 * style was wrong.
 *
 * So these read getComputedStyle and do the contrast arithmetic.
 */

import { test, expect, type Page } from '@playwright/test';

/** WCAG relative luminance. */
function luminance(rgb: [number, number, number]): number {
  const [r, g, b] = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: [number, number, number], b: [number, number, number]): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function parseRgb(value: string): [number, number, number] {
  const m = value.match(/rgba?\(([^)]+)\)/);
  if (!m) throw new Error(`cannot parse colour: ${value}`);
  const [r, g, b] = m[1].split(',').map((n) => parseFloat(n));
  return [r, g, b];
}

/** Colour, and the first ancestor background that is not transparent. */
async function inkOn(page: Page, selector: string) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const color = getComputedStyle(el).color;
    let node: Element | null = el;
    let bg = 'rgba(0, 0, 0, 0)';
    while (node) {
      const c = getComputedStyle(node).backgroundColor;
      if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) { bg = c; break; }
      node = node.parentElement;
    }
    return { color, bg, text: (el.textContent || '').trim().slice(0, 40) };
  }, selector);
}

test.describe('Design invariants', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('the primary CTA is readable on its own background', async ({ page }) => {
    const seen = await inkOn(page, '.btn--primary');
    expect(seen, '.btn--primary not found on the landing page').not.toBeNull();

    const ratio = contrast(parseRgb(seen!.color), parseRgb(seen!.bg));
    expect(
      ratio,
      `"${seen!.text}" renders ${seen!.color} on ${seen!.bg} — ${ratio.toFixed(2)}:1. ` +
      `This is how the accent-coloured <a> rule leaking past .btn--primary shows up.`,
    ).toBeGreaterThanOrEqual(4.5);
  });

  test('body copy clears WCAG AA against the paper', async ({ page }) => {
    for (const sel of ['.hero__lede', '.section-lede', '.seq__note']) {
      const seen = await inkOn(page, sel);
      if (!seen) continue;
      const ratio = contrast(parseRgb(seen.color), parseRgb(seen.bg));
      expect(ratio, `${sel} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
    }
  });

  test('nav links keep the chrome colour, not the link accent', async ({ page }) => {
    const link = await inkOn(page, '#main-nav .hc-nav__link:not([aria-current])');
    expect(link).not.toBeNull();
    // --signal is #1B2FBF. The nav is deliberately quiet; if the base anchor
    // rule wins again, every item turns ultramarine.
    expect(
      parseRgb(link!.color),
      `nav links render ${link!.color}; expected the muted chrome ink`,
    ).not.toEqual([27, 47, 191]);
  });

  test('an in-page jump is not hidden under the sticky masthead', async ({ page }) => {
    const navHeight = await page.evaluate(
      () => document.querySelector('#main-nav')!.getBoundingClientRect().height);
    const padding = await page.evaluate(
      () => parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 0);
    expect(
      padding,
      `scroll-padding-top is ${padding}px but the sticky masthead is ${navHeight}px; ` +
      `anchor targets land underneath it`,
    ).toBeGreaterThanOrEqual(navHeight);
  });

  test('nothing on the page scrolls sideways', async ({ page }) => {
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `horizontal overflow at ${width}px`).toBeLessThanOrEqual(1);
    }
  });

  test('every tap target on the page clears 44px', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 900 });
    const small = await page.evaluate(() => {
      const out: string[] = [];
      for (const el of document.querySelectorAll('a.btn, button, input[type="submit"]')) {
        const r = el.getBoundingClientRect();
        if (r.height > 0 && r.height < 44) {
          out.push(`${el.tagName.toLowerCase()}.${el.className} ${Math.round(r.height)}px`);
        }
      }
      return out;
    });
    expect(small, 'controls under the 44px minimum').toEqual([]);
  });
});
