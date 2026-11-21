"""Browser-measured Auto Fit support for KadrFarm Professional Renderer V2.

The browser is the source of truth for text metrics. This module supplies a
small deterministic fitting routine; it does not rewrite copy or alter scene
meaning. The pipeline runs it during an all-shots preflight before recording.
"""
from __future__ import annotations

from typing import Any


# Playwright evaluates this function after document.fonts.ready and media load.
# It changes only presentation styles. Authored text remains unchanged except
# temporarily substituting final states of count_up/update_text events while
# measuring, then restoring the original text.
AUTOFIT_SCRIPT = r"""(options) => {
  const stage = document.querySelector('.scene-canvas');
  if (!stage) throw new Error('AutoFit: .scene-canvas is missing');
  const variants = Array.isArray(options?.variants) ? options.variants : [];
  const stageRect = stage.getBoundingClientRect();
  const report = [];
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const numeric = (value) => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const rectOf = (element) => {
    const r = element.getBoundingClientRect();
    return {left:r.left, top:r.top, right:r.right, bottom:r.bottom,
      width:r.width, height:r.height};
  };
  const intersects = (a, b) => a.left < b.right && a.right > b.left &&
    a.top < b.bottom && a.bottom > b.top;

  function configFor(object) {
    if (object.classList.contains('object-text')) return {
      kind:'text', container:'.type-layout',
      parts:[['.type-eyebrow',14],['.type-main',24],['.type-subtitle',16]],
      minScale:0.60, spacingPower:1.65
    };
    if (object.classList.contains('object-callout')) return {
      kind:'callout', container:'.callout',
      parts:[['.callout-eyebrow',12],['.callout-copy',18]],
      minScale:0.62, spacingPower:2.0
    };
    if (object.classList.contains('object-counter')) return {
      kind:'counter', container:'.counter',
      parts:[['.counter-value',40],['.counter-label',16]],
      minScale:0.62, spacingPower:1.8
    };
    if (object.classList.contains('object-formula')) return {
      kind:'formula', container:'.formula',
      parts:[['.formula-expression',22],['.formula-caption',13]],
      minScale:0.62, spacingPower:1.8
    };
    if (object.classList.contains('object-table')) return {
      kind:'table', container:'.table-shell',
      parts:[['.editorial-table th',12],['.editorial-table td',12]],
      minScale:0.68, spacingPower:1.8
    };
    if (object.classList.contains('object-node_graph')) return {
      kind:'node_graph', container:'.graph-stage', extraContainers:['.graph-node'],
      parts:[['.graph-node-title',14],['.graph-node-subtitle',11]],
      minScale:0.64, spacingPower:1.8
    };
    if (object.classList.contains('object-chart')) return {
      kind:'chart', container:'.chart-card', extraContainers:['.chart-heading'],
      parts:[['.chart-title',16],['.chart-subtitle',12],['.chart-axis-label',11],
        ['.legend-item',11],['.chart-disclosure',10],['.donut-total',22]],
      minScale:0.64, spacingPower:1.8
    };
    return null;
  }

  function collectStateVariants(objectId, object, parts) {
    const result = [];
    for (const variant of variants) {
      if (String(variant.target_id) !== objectId) continue;
      const selector = String(variant.selector || '');
      const node = selector ? object.querySelector(selector) : object;
      if (!node) {
        result.push({selector, text:String(variant.text ?? ''), missing:true});
      } else {
        result.push({node, selector, text:String(variant.text ?? ''), missing:false});
      }
    }
    return result;
  }

  function applyScaledStyles(container, parts, baseline, scale, spacingPower) {
    const spacing = Math.pow(scale, spacingPower);
    const scaleContainer = (element, base) => {
      if (!element || !base) return;
      for (const prop of ['paddingTop','paddingRight','paddingBottom','paddingLeft','gap','rowGap','columnGap']) {
        if (base[prop] !== null && base[prop] !== undefined) {
          element.style[prop] = `${Math.max(0, base[prop] * spacing)}px`;
        }
      }
    };
    if (container) scaleContainer(container, baseline.container);
    for (const extra of baseline.extraContainers || []) {
      scaleContainer(extra.element, extra);
    }
    for (const item of baseline.parts) {
      const element = item.element;
      element.style.fontSize = `${Math.max(item.minFont, item.fontSize * scale)}px`;
      for (const prop of ['marginTop','marginRight','marginBottom','marginLeft']) {
        if (item[prop] !== null && item[prop] !== undefined) {
          element.style[prop] = `${Math.max(0, item[prop] * spacing)}px`;
        }
      }
      if (item.letterSpacing !== null && Number.isFinite(item.letterSpacing)) {
        element.style.letterSpacing = `${item.letterSpacing * scale}px`;
      }
      if (item.lineHeight !== null && item.lineHeight > 0) {
        element.style.lineHeight = `${item.lineHeight * scale}px`;
      }
      if (item.paddingTop !== null) {
        element.style.paddingTop = `${Math.max(0, item.paddingTop * spacing)}px`;
        element.style.paddingRight = `${Math.max(0, item.paddingRight * spacing)}px`;
        element.style.paddingBottom = `${Math.max(0, item.paddingBottom * spacing)}px`;
        element.style.paddingLeft = `${Math.max(0, item.paddingLeft * spacing)}px`;
      }
    }
  }

  function measureFit(object, container, textElements) {
    const objectRect = rectOf(object);
    const failures = [];
    for (const element of textElements) {
      if (!String(element.textContent || '').trim()) continue;
      const rect = rectOf(element);
      if (rect.width < 1 || rect.height < 1) {
        failures.push('zero_sized_text');
        continue;
      }
      const eps = 0.5;
      if (rect.left < objectRect.left - eps || rect.right > objectRect.right + eps ||
          rect.top < objectRect.top - eps || rect.bottom > objectRect.bottom + eps) {
        failures.push('text_outside_object');
      }
      // Honour every actual clipping ancestor, but do not treat visible glyph
      // overflow of an element with overflow:visible as clipping.
      let ancestor = element.parentElement;
      while (ancestor && ancestor !== object.parentElement) {
        const style = getComputedStyle(ancestor);
        const ar = rectOf(ancestor);
        const clipsX = ['hidden','clip','scroll','auto'].includes(style.overflowX);
        const clipsY = ['hidden','clip','scroll','auto'].includes(style.overflowY);
        if ((clipsX && (rect.left < ar.left - eps || rect.right > ar.right + eps)) ||
            (clipsY && (rect.top < ar.top - eps || rect.bottom > ar.bottom + eps))) {
          failures.push('ancestor_clipping');
        }
        ancestor = ancestor.parentElement;
      }
      const ownStyle = getComputedStyle(element);
      const ownClipX = ['hidden','clip','scroll','auto'].includes(ownStyle.overflowX);
      const ownClipY = ['hidden','clip','scroll','auto'].includes(ownStyle.overflowY);
      if ((ownClipX && element.scrollWidth > element.clientWidth + 2) ||
          (ownClipY && element.scrollHeight > element.clientHeight + 2)) {
        failures.push('text_self_clipping');
      }
    }
    if (container) {
      const style = getComputedStyle(container);
      if ((['hidden','clip','scroll','auto'].includes(style.overflowX) && container.scrollWidth > container.clientWidth + 2) ||
          (['hidden','clip','scroll','auto'].includes(style.overflowY) && container.scrollHeight > container.clientHeight + 2)) {
        failures.push('component_container_clipping');
      }
    }
    return {fits:failures.length === 0, failures:[...new Set(failures)]};
  }

  const eligible = [...document.querySelectorAll('.scene-object')];
  for (const object of eligible) {
    const config = configFor(object);
    if (!config) continue;
    const objectId = String(object.dataset.objectId || '(unnamed)');
    const container = object.querySelector(config.container);
    if (!container) continue;
    const textElements = [];
    const baselineParts = [];
    let minimumScale = config.minScale;
    for (const [selector, minFont] of config.parts) {
      for (const element of object.querySelectorAll(selector)) {
        if (!String(element.textContent || '').trim()) continue;
        const style = getComputedStyle(element);
        const fontSize = numeric(style.fontSize);
        if (!fontSize) continue;
        textElements.push(element);
        // Never enlarge copy merely to satisfy a role floor: the authored
        // size is a hard upper bound for this adaptation pass.
        const effectiveMinFont = Math.min(minFont, fontSize);
        baselineParts.push({
          selector, element, fontSize, minFont:effectiveMinFont,
          marginTop:numeric(style.marginTop), marginRight:numeric(style.marginRight),
          marginBottom:numeric(style.marginBottom), marginLeft:numeric(style.marginLeft),
          letterSpacing:style.letterSpacing === 'normal' ? null : numeric(style.letterSpacing),
          lineHeight:style.lineHeight === 'normal' ? null : numeric(style.lineHeight),
          paddingTop:numeric(style.paddingTop), paddingRight:numeric(style.paddingRight),
          paddingBottom:numeric(style.paddingBottom), paddingLeft:numeric(style.paddingLeft)
        });
        minimumScale = Math.max(minimumScale, effectiveMinFont / fontSize);
      }
    }
    if (!textElements.length) continue;
    minimumScale = clamp(minimumScale, config.minScale, 1);
    const readSpacing = (element) => {
      const style = getComputedStyle(element);
      return {
        element,
        paddingTop:numeric(style.paddingTop),
        paddingRight:numeric(style.paddingRight),
        paddingBottom:numeric(style.paddingBottom),
        paddingLeft:numeric(style.paddingLeft),
        gap:numeric(style.gap),
        rowGap:numeric(style.rowGap),
        columnGap:numeric(style.columnGap)
      };
    };
    const baseline = {
      container: readSpacing(container),
      extraContainers: (config.extraContainers || []).flatMap(selector =>
        [...object.querySelectorAll(selector)].map(readSpacing)
      ),
      parts:baselineParts
    };
    const stateVariants = collectStateVariants(objectId, object, config.parts);
    const originalText = stateVariants.filter(v => !v.missing).map(v => ({node:v.node, value:v.node.textContent}));

    const fitsAt = (scale) => {
      applyScaledStyles(container, textElements, baseline, scale, config.spacingPower);
      const states = [null, ...stateVariants];
      for (const state of states) {
        if (state && state.missing) return false;
        if (state) state.node.textContent = state.text;
        const measured = measureFit(object, container, textElements);
        if (state) {
          const saved = originalText.find(item => item.node === state.node);
          if (saved) state.node.textContent = saved.value;
        }
        if (!measured.fits) return false;
      }
      return true;
    };

    const initial = measureFit(object, container, textElements);
    let appliedScale = 1;
    let fits = fitsAt(1);
    if (!fits) {
      if (fitsAt(minimumScale)) {
        let low = minimumScale;
        let high = 1;
        for (let iteration = 0; iteration < 12; iteration += 1) {
          const middle = (low + high) / 2;
          if (fitsAt(middle)) low = middle;
          else high = middle;
        }
        appliedScale = low;
        fits = true;
      } else {
        appliedScale = minimumScale;
        fitsAt(appliedScale);
        fits = false;
      }
    }
    // Restore authored/current text after all alternate event states were tested.
    for (const item of originalText) item.node.textContent = item.value;
    applyScaledStyles(container, textElements, baseline, appliedScale, config.spacingPower);
    const finalMeasurement = measureFit(object, container, textElements);
    fits = fits && finalMeasurement.fits;

    const finalFontSizes = {};
    for (const item of baselineParts) {
      finalFontSizes[item.selector] = Number(numeric(getComputedStyle(item.element).fontSize).toFixed(2));
    }
    report.push({
      object_id:objectId,
      kind:config.kind,
      status:fits ? (appliedScale < 0.999 ? 'fitted' : 'unchanged') : 'unresolved',
      scale:Number(appliedScale.toFixed(4)),
      minimum_scale:Number(minimumScale.toFixed(4)),
      final_font_sizes_px:finalFontSizes,
      spacing_scale:Number(Math.pow(appliedScale, config.spacingPower).toFixed(4)),
      states_checked:1 + stateVariants.length,
      remaining_failures:finalMeasurement.failures
    });
  }

  const unresolved = report.filter(item => item.status === 'unresolved');
  return {
    schema_version:'1.0',
    engine:'chromium_measured_auto_fit_v1_1',
    object_count:report.length,
    fitted_count:report.filter(item => item.status === 'fitted').length,
    unchanged_count:report.filter(item => item.status === 'unchanged').length,
    unresolved_count:unresolved.length,
    objects:report
  };
}"""


def build_layout_variants(shot: Any) -> list[dict[str, str]]:
    """Return final text states that must fit before capture."""
    variants: list[dict[str, str]] = []
    for event in getattr(shot, "events", []):
        action = str(event.action)
        params = dict(getattr(event, "params", {}) or {})
        if action == "update_text":
            if "text" not in params:
                continue
            variants.append({
                "target_id": str(event.target_id),
                "selector": ".type-main,.callout-copy,.counter-value",
                "text": "" if params["text"] is None else str(params["text"]),
                "event_id": str(event.event_id),
                "action": action,
            })
        elif action == "count_up":
            try:
                decimals = max(0, min(6, int(params.get("decimals", 0))))
                endpoints = ("from", "to")
                values = [(name, float(params.get(name, 0))) for name in endpoints]
            except (TypeError, ValueError, OverflowError):
                continue
            suffix = str(params.get("suffix", ""))
            for endpoint, value in values:
                if not math_is_finite(value):
                    continue
                variants.append({
                    "target_id": str(event.target_id),
                    "selector": ".counter-value",
                    "text": f"{value:.{decimals}f}{suffix}",
                    "event_id": f"{event.event_id}:{endpoint}",
                    "action": action,
                })
    return variants


def math_is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))
