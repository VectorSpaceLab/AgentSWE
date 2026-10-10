# Choose a Safe Inspection Route

## Case

- Case ID: `test_001`
- Application: [Arbor Facility Route Board](assets/serve.py)
- Viewport: 1280 x 800 pixels
- Corpus: closed. Route geometry, hazard hatching, and the legend are authoritative only as rendered on the canvas.

## Goal

Plan an inspection from `Receiving` to `Lab 3`. On the canvas, choose the shortest continuous route that does not cross red hazard hatching and passes a blue eyewash station. The runtime layout changes at fixture launch. Click the route segments in travel order, then choose inspection kit `Chemical splash`, assign `Dana Okafor`, and set completion window `45 minutes`.

Review the generated itinerary. If the application reports a discontinuity or hazard intersection, clear the route and correct it from the current canvas rather than submitting an invalid plan.

## Completion

Confirm once from the review page. The receipt must show Receiving to Lab 3, a continuous safe route, at least one eyewash station, Dana Okafor, Chemical splash, and 45 minutes. Do not select or traverse a route solely from DOM labels or static source; the decisive route facts are visual pixels.
