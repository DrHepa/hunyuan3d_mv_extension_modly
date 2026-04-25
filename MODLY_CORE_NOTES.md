# Modly core notes for Hunyuan3D-2mv

This extension is now prepared to consume one required front image and optional
left, back, and right views, but current Modly model workflow execution still
needs a core change to preserve named image inputs.

## Current state

- `DrHepa/modly@modly-private` already supports image artifacts and image previews.
- The Hunyuan3D-2mv manifest declares named image inputs: `front`, `left`,
  `back`, and `right`.
- The extension generator accepts optional views through params:
  `left_image_path`, `back_image_path`, `right_image_path`.
- The generator also accepts optional base64 payloads through:
  `left_image`, `back_image`, `right_image` with `<id>_is_b64`.

## Blocking issue

The model workflow runner currently selects image inputs mainly by output type.
For multi-input models this collapses several connected image nodes into one
model upload and loses the port name that was connected in the workflow.

For models with named `inputs[]`, the runner must preserve the edge
`targetHandle` and map each upstream image to its target port.

## Recommended core change

When running a model node with named image inputs:

1. Read incoming edges for the model node.
2. Resolve each image source by the edge `targetHandle`.
3. Use the image connected to `front` as the primary upload for
   `/generate/from-image`.
4. Pass the remaining image file paths in `params`:
   - `left` -> `left_image_path`
   - `back` -> `back_image_path`
   - `right` -> `right_image_path`

This keeps compatibility with the current image-to-model endpoint while letting
multi-view model extensions receive all connected images.

## Generic future contract

A more general contract would mirror process extensions and pass a named input
map:

```json
{
  "__inputs": {
    "front": { "type": "image", "filePath": "...", "sourceNodeId": "..." },
    "left": { "type": "image", "filePath": "...", "sourceNodeId": "..." },
    "back": { "type": "image", "filePath": "...", "sourceNodeId": "..." },
    "right": { "type": "image", "filePath": "...", "sourceNodeId": "..." }
  }
}
```

The extension can work with the simpler `*_image_path` params now and can be
extended later to read `params.__inputs` if Modly adopts the generic contract.

## Acceptance checks

- A model node with four named image ports keeps each edge mapped by
  `targetHandle`.
- `front` is the primary upload sent to `/generate/from-image`.
- `left`, `back`, and `right` are passed as params paths when connected.
- A front-only workflow still works.
- Image artifact output and preview behavior remain unchanged.
