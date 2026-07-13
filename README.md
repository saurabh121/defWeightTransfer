# Deformer Weight Tool for Maya

A fast, artist-friendly Maya tool for **transferring, mirroring, and converting deformer weights** between meshes — built for production rigging workflows on high-poly characters.

Copying painted deformer weights (bend, cluster, FFD, nonLinear, etc.) from one mesh to another in Maya is normally slow and manual. This tool automates the whole pipeline — and because all weight I/O is **batched through the OpenMaya API and sliced attribute writes**, it stays fast even on meshes with hundreds of thousands of vertices (seconds instead of minutes).

![Maya](https://img.shields.io/badge/Maya-2024%2B-37A5CC) ![Python](https://img.shields.io/badge/Python-3.x-3776AB) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

### 1. Deformer Weights — copy between meshes
Copy painted deformer weights from a source mesh to any number of target meshes, even with **different topology** (closest-point surface association via `copySkinWeights` under the hood).

- Multiple deformers × multiple targets in one click
- Automatically wires the deformer chain into each target's deformation history (works on skinned and non-skinned targets)
- Save / load weights to JSON for backup or transfer between scenes

### 2. Mirror Weights — asymmetry-safe L→R mirror
Mirror deformer weights across an axis, with **closest-point mapping** so it works on meshes that are not perfectly symmetric.

- Exact-match fast pass via spatial hashing (near-instant on symmetric meshes)
- `MMeshIntersector`-accelerated closest-point fallback for asymmetric areas
- Automatic `L_` ↔ `R_` deformer/influence name remapping

### UI
- Tabbed window with per-tab **progress bars**
- Clear, specific error messages (what's wrong and how to fix it)
- Automatic JSON backups of every transfer in the system temp folder

---

## Performance

All per-vertex `cmds.skinPercent` / `cmds.percent` loops were replaced with batched operations:

| Operation | Method |
|---|---|
| Read/write skin weights | `MFnSkinCluster.getWeights / setWeights` — one call for the whole mesh |
| Read deformer weights | one `cmds.percent` query on `vtx[*]` |
| Write deformer weights | one sliced `setAttr` on `weightList[i].weights[0:n]` |
| Mirror vertex mapping | spatial-hash exact match + `MMeshIntersector` |

Typical result: a transfer that took **minutes** on a 100k+ vertex mesh completes in **seconds**.

---

## Installation

1. Copy `ss_copyDefWeightFromOther.py` to your Maya scripts folder
   (e.g. `Documents/maya/<version>/scripts/`).
2. In the Maya Script Editor (Python tab):

```python
import ss_copyDefWeightFromOther as defWeight
defWeight.ss_defWeightToolUI()
```

Optionally middle-drag that snippet to a shelf button.

---

## Quick usage

**Copy deformer weights**
1. *Deformer Weights* tab → select your deformer node(s) → `<< Add Selected Deformers`
2. Set the **Source Obj** (mesh that already has the painted weights)
3. Select target meshes → `<< Add Selected to Target List`
4. `Copy Weights to Targets`

**Mirror deformer weights**
1. *Mirror Weights* tab → add the left-side deformer(s) (`L_*`) to the mirror list
2. Set the mirror source mesh → `Mirror Deformer Weights`
   (weights land on the matching `R_*` deformer)

---

## Requirements

- Autodesk Maya 2024+ (Python 3, `maya.api.OpenMaya` 2.0)
- No external dependencies

## Notes

- Every transfer writes a JSON backup to your system temp folder (`%TEMP%` on Windows), named per deformer/mesh.
- Weight transfer between different topologies uses closest-point association — results are best when meshes occupy the same world space.
- *Def to Skin* renormalizes the other influences in Python and does not respect influence locks.

## License

MIT — free to use and modify. Credit appreciated.
