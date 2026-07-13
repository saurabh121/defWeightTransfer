import maya.cmds as cmds
import json
import os
import tempfile
import re
import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma




def _get_skin_cluster(node):
    """Return the first skinCluster in history of a transform or shape."""
    if cmds.nodeType(node) == "transform":
        shapes = cmds.listRelatives(node, s=True, ni=True, f=True) or []
    else:
        shapes = [node]
    for shp in shapes:
        for h in cmds.listHistory(shp, pdo=True) or []:
            if cmds.nodeType(h) == "skinCluster":
                return h
    return None


def _ensure_influence(skin, jnt):
    """Make sure jnt is an influence on skin; add at 0.0 if not present."""
    infs = cmds.skinCluster(skin, q=True, inf=True) or []
    if jnt not in infs:
        cmds.skinCluster(skin, e=True, ai=jnt, lw=False, wt=0.0)


def _get_shape_dag_and_comp(shape):
    """
    Return (dagPath, component) for all verts of the given shape.
    This version targets polygon meshes.
    """
    sel = om.MSelectionList()
    sel.add(shape)
    dagPath = sel.getDagPath(0)
    # Build a component covering all vertices
    fnMesh = om.MFnMesh(dagPath)
    v_count = fnMesh.numVertices
    comp_fn = om.MFnSingleIndexedComponent()
    comp = comp_fn.create(om.MFn.kMeshVertComponent)
    comp_fn.addElements(list(range(v_count)))
    return dagPath, comp


# ---------- fast weight IO helpers (batched, no per-vertex cmds calls) ----------

def _shape_of(geo):
    """Return the non-intermediate shape (full path) of a transform, or geo itself if already a shape."""
    if cmds.nodeType(geo) == "transform":
        shapes = cmds.listRelatives(geo, s=True, ni=True, f=True) or []
        if not shapes:
            cmds.error("No shape under transform: {}".format(geo))
        return shapes[0]
    return cmds.ls(geo, l=True)[0]


def _skin_fn(skin):
    sel = om.MSelectionList()
    sel.add(skin)
    return oma.MFnSkinCluster(sel.getDependNode(0))


def _skin_influence_index(mfnSkin, joint):
    """Column index of joint in the skinCluster weight array."""
    short = joint.split("|")[-1]
    inf_names = [om.MFnDagNode(p).fullPathName() for p in mfnSkin.influenceObjects()]
    for idx, full in enumerate(inf_names):
        if full.split("|")[-1] == short:
            return idx
    for idx, full in enumerate(inf_names):
        if full.endswith(short):
            return idx
    return None


def ss_getSkinWeights(geo, skin=None):
    """
    Read ALL skin weights in one API call.
    Returns (mfnSkin, dagPath, comp, weights, inf_count) where weights is flat:
    [v0_inf0, v0_inf1, ..., v1_inf0, v1_inf1, ...]
    """
    shape = _shape_of(geo)
    skin = skin or _get_skin_cluster(shape)
    if not skin:
        cmds.error("No skinCluster found on {}".format(geo))
    mfnSkin = _skin_fn(skin)
    dagPath, comp = _get_shape_dag_and_comp(shape)
    weights, inf_count = mfnSkin.getWeights(dagPath, comp)
    return mfnSkin, dagPath, comp, weights, inf_count


def ss_setSkinWeights(mfnSkin, dagPath, comp, inf_count, weights, normalize=True):
    """Write ALL skin weights in one API call. weights = flat list or MDoubleArray."""
    inf_indices = om.MIntArray(range(inf_count))
    mfnSkin.setWeights(dagPath, comp, inf_indices, om.MDoubleArray(weights), normalize)


def _deformer_geometry_index(defNode, geo):
    """
    weightList[] multi index of geo on defNode.
    Tries the normal deformer query first, then traces the manually wired
    chains created by ss_transforInputFronObject (downstream and upstream).
    """
    shape = _shape_of(geo)
    shape_tail = shape.split("|")[-1]

    # 1) Normal deformer membership
    geos = cmds.deformer(defNode, q=True, geometry=True) or []
    idxs = cmds.deformer(defNode, q=True, geometryIndices=True) or []
    for g, i in zip(geos, idxs):
        if g.split("|")[-1] == shape_tail:
            return i

    # 2) Walk each outputGeometry[i] downstream until a shape is reached
    out_ids = cmds.getAttr(defNode + ".outputGeometry", multiIndices=True) or []
    for i in out_ids:
        node, idx = defNode, i
        for _ in range(64):  # guard against cycles
            plugs = cmds.listConnections("{}.outputGeometry[{}]".format(node, idx),
                                         d=True, s=False, p=True, sh=True) or []
            if not plugs:
                break
            dest_node = plugs[0].split(".")[0]
            if cmds.objectType(dest_node, isAType="shape"):
                if dest_node.split("|")[-1] == shape_tail:
                    return i
                break
            m = re.search(r"\.input\[(\d+)\]", plugs[0])
            if m is None or not cmds.objectType(dest_node, isAType="geometryFilter"):
                break
            node, idx = dest_node, int(m.group(1))

    # 3) Check each input[i].inputGeometry source against the shape's history
    hist_tails = set(h.split("|")[-1] for h in (cmds.listHistory(shape) or []))
    in_ids = cmds.getAttr(defNode + ".input", multiIndices=True) or []
    for i in in_ids:
        srcs = cmds.listConnections("{}.input[{}].inputGeometry".format(defNode, i),
                                    s=True, d=False) or []
        if srcs and srcs[0].split("|")[-1] in hist_tails:
            return i
    return None


def ss_getDefWeights(defNode, geo):
    """
    Read ALL deformer weights of geo in one call (instead of per-vertex cmds.percent).
    Returns a list of floats, one per vertex.
    """
    vtx_count = cmds.polyEvaluate(geo, vertex=True)
    try:
        vals = cmds.percent(defNode, "{}.vtx[*]".format(geo), q=True, v=True)
    except Exception:
        vals = None
    if vals is not None and len(vals) == vtx_count:
        return [float(v) for v in vals]

    # Slow but safe fallback (e.g. partial deformer membership)
    cmds.warning("Batch weight query failed on {} / {} - using per-vertex fallback.".format(defNode, geo))
    out = []
    for i in range(vtx_count):
        try:
            w = cmds.percent(defNode, "{}.vtx[{}]".format(geo, i), q=True, v=True)
            out.append(float(w[0]) if w else 0.0)
        except Exception:
            out.append(0.0)
    return out


def ss_setDefWeights(defNode, geo, weights, default=1.0):
    """
    Write ALL deformer weights of geo in ONE setAttr call on weightList[gi].weights.
    weights: list of per-vertex floats, or dict {vertexIndex(str/int): weight}.
    """
    vtx_count = cmds.polyEvaluate(geo, vertex=True)
    if isinstance(weights, dict):
        vals = [default] * vtx_count
        for k, w in weights.items():
            idx = int(k)
            if 0 <= idx < vtx_count:
                vals[idx] = float(w)
    else:
        vals = [float(w) for w in weights]
        if len(vals) != vtx_count:
            cmds.error("Weight count {} does not match vertex count {} on {}".format(
                len(vals), vtx_count, geo))

    gi = _deformer_geometry_index(defNode, geo)
    if gi is not None:
        cmds.setAttr("{}.weightList[{}].weights[0:{}]".format(defNode, gi, vtx_count - 1),
                     *vals, size=vtx_count)
    else:
        # Slow but safe fallback
        cmds.warning("Could not resolve geometry index of {} on {} - using per-vertex fallback.".format(geo, defNode))
        for i, w in enumerate(vals):
            cmds.percent(defNode, "{}.vtx[{}]".format(geo, i), v=w)


def ss_shift_skin_weights(
    geo_or_shape,
    source_joint,
    target_joint,
    skin_cluster=None,
    delete_source=False,
):
    """
    Move all skin weight from source_joint to target_joint on the given geometry.
    - geo_or_shape: transform or shape name (mesh)
    - source_joint, target_joint: joint names
    - skin_cluster: optional skinCluster node; if None it is auto-detected
    - delete_source: if True, remove source_joint from skinCluster after transfer
    """

    # Resolve shape
    if cmds.nodeType(geo_or_shape) == "transform":
        shapes = cmds.listRelatives(geo_or_shape, s=True, ni=True, f=True) or []
        if not shapes:
            cmds.error("No shape under transform: {}".format(geo_or_shape))
        shape = shapes[0]
    else:
        shape = geo_or_shape

    # Resolve skinCluster
    skin = skin_cluster or _get_skin_cluster(shape)
    if not skin:
        cmds.error("No skinCluster found on {}".format(geo_or_shape))

    # Make sure joints exist
    if not cmds.objExists(source_joint):
        cmds.error("Source joint does not exist: {}".format(source_joint))
    if not cmds.objExists(target_joint):
        cmds.error("Target joint does not exist: {}".format(target_joint))

    # Ensure target is an influence (at weight 0.0)
    _ensure_influence(skin, target_joint)

    # Build API wrappers
    sel = om.MSelectionList()
    sel.add(skin)
    skinObj = sel.getDependNode(0)
    mfnSkin = oma.MFnSkinCluster(skinObj)

    # Full influence list (MDagPath array)
    inf_paths = mfnSkin.influenceObjects()
    inf_names = [om.MFnDagNode(p).fullPathName() for p in inf_paths]

    # Map to indices used in the weight array (column indices)
    # NOTE: cmds returns short names; match by full path endings to be robust.
    def _find_index(j):
        # Prefer exact match by full path tail
        short = cmds.ls(j, sn=True)[0]
        # Try full path tail match first, else substring
        for idx, full in enumerate(inf_names):
            if full.split("|")[-1] == short:
                return idx
        # Fallback: loose endswith
        for idx, full in enumerate(inf_names):
            if full.endswith(short):
                return idx
        return None

    src_idx = _find_index(source_joint)
    tgt_idx = _find_index(target_joint)

    if src_idx is None:
        cmds.error("Source joint is not an influence on {}: {}".format(skin, source_joint))
    if tgt_idx is None:
        # If we just added it, refresh influenceObjects and retry
        inf_paths = mfnSkin.influenceObjects()
        inf_names = [om.MFnDagNode(p).fullPathName() for p in inf_paths]
        tgt_idx = _find_index(target_joint)
        if tgt_idx is None:
            cmds.error("Failed to add/resolve target influence: {}".format(target_joint))

    # Build component over all vertices (mesh variant)
    dagPath, comp = _get_shape_dag_and_comp(shape)

    # Read all weights: flattened [v0_all_infs, v1_all_infs, ...]
    weights, inf_count = mfnSkin.getWeights(dagPath, comp)
    inf_indices = om.MIntArray(range(inf_count))

    # Fast in-place transfer: target += source; source = 0
    # weights is MDoubleArray; edit by index
    vtx_count = int(len(weights) / inf_count)
    for v in range(vtx_count):
        base = v * inf_count
        w_src = weights[base + src_idx]
        if w_src != 0.0:
            weights[base + tgt_idx] = weights[base + tgt_idx] + w_src
            weights[base + src_idx] = 0.0

    # Write back (normalize=True)
    mfnSkin.setWeights(dagPath, comp, inf_indices, weights, True)

    # Optionally remove the source influence entirely
    if delete_source:
        # Removing will renormalize remaining influences
        cmds.skinCluster(skin, e=True, ri=source_joint)

    return skin



# ---------- helpers ----------
def _dagPath_from_mesh_name(mesh):
    """Return a non-intermediate mesh MDagPath from a transform or shape name."""
    sel = om.MSelectionList(); sel.add(mesh)
    dag = sel.getDagPath(0)
    if dag.apiType() == om.MFn.kTransform:
        dfn = om.MFnDagNode(dag)
        for i in range(dfn.childCount()):
            ch = dfn.child(i)
            if ch.hasFn(om.MFn.kMesh):
                sp = om.MDagPath.getAPathTo(ch)
                try:
                    if not cmds.getAttr(om.MFnDagNode(sp).fullPathName()+".intermediateObject"):
                        return sp
                except Exception:
                    return sp
        dag.extendToShape()  # fallback
    # if shape was passed and is intermediate, go up then retry
    try:
        if cmds.getAttr(om.MFnDagNode(dag).fullPathName()+".intermediateObject"):
            dag.pop()
            return _dagPath_from_mesh_name(om.MFnDagNode(dag).fullPathName())
    except Exception:
        pass
    return dag


def _dist2(a, b):
    v = a - b            # MVector
    return v * v         # dot product == squared length


def _find_skin_cluster(mesh):
    hist = cmds.listHistory(mesh, pruneDagObjects=True) or []
    skins = [h for h in hist if cmds.nodeType(h) == "skinCluster"]
    if not skins:
        raise RuntimeError("No skinCluster found on: {}".format(mesh))
    return skins[0]

# ---------- main ----------
def ss_mirror_skin_weights(mesh,
                        axis="x",
                        center=0.0,
                        side="LtoR",
                        tol=1e-4,
                        do_influence_remap=True,
                        left_tags=("L_", ".L"),
                        right_tags=("R_", ".R")):
    """
    Asymmetry-safe mirror using closest-point mapping + optional L<->R influence remap.
    """
    if axis not in ("x","y","z"):
        raise ValueError('axis must be "x", "y" or "z"')
    if side not in ("LtoR","RtoL"):
        raise ValueError('side must be "LtoR" or "RtoL"')

    # Resolve MDagPath & API fns
    dag = _dagPath_from_mesh_name(mesh)
    fn_mesh = om.MFnMesh(dag)

    skin = _find_skin_cluster(mesh)
    sel = om.MSelectionList(); sel.add(skin)
    fn_skin = oma.MFnSkinCluster(sel.getDependNode(0))

    # Influences (ordered as API returns)
    inf_paths = fn_skin.influenceObjects()
    inf_names = [p.fullPathName().split("|")[-1] for p in inf_paths]
    inf_count = len(inf_names)
    if inf_count == 0:
        raise RuntimeError("Skin has no influences: {}".format(skin))

    # Build L<->R mapping once
    def flip_name(n):
        for L, R in zip(left_tags, right_tags):
            if L in n: return n.replace(L, R)
            if R in n: return n.replace(R, L)
        return n
    if do_influence_remap:
        partner_of = {n: flip_name(n) for n in inf_names}
    else:
        partner_of = {n: n for n in inf_names}

    src_to_tgt_inf_idx = []
    for n in inf_names:
        pn = partner_of.get(n, n)
        src_to_tgt_inf_idx.append(inf_names.index(pn) if pn in inf_names else inf_names.index(n))

    # Matrices and points
    M = dag.inclusiveMatrix()
    Minv = dag.inclusiveMatrixInverse()
    pts_obj = fn_mesh.getPoints(om.MSpace.kObject)
    pts_wld = [p * M for p in pts_obj]
    ids = list(range(len(pts_obj)))

    ax = {"x":0,"y":1,"z":2}[axis]
    if side == "LtoR":
        is_src = lambda pw: (pw[ax] >= center + tol)
        is_tgt = lambda pw: (pw[ax] <= center - tol)
    else:  # RtoL
        is_src = lambda pw: (pw[ax] <= center - tol)
        is_tgt = lambda pw: (pw[ax] >= center + tol)

    src_ids  = [i for i in ids if is_src(pts_wld[i])]
    tgt_ids  = [i for i in ids if is_tgt(pts_wld[i])]
    seam_ids = [i for i in ids if abs(pts_wld[i][ax] - center) < tol]

    # Fetch all weights in one go
    comp_all_fn = om.MFnSingleIndexedComponent()
    comp_all = comp_all_fn.create(om.MFn.kMeshVertComponent)
    comp_all_fn.addElements(ids)
    flat_wts, _ = fn_skin.getWeights(dag, comp_all)
    def slice_w(i):
        return flat_wts[i*inf_count:(i+1)*inf_count]

    # Helpers
    it_poly = om.MItMeshPolygon(dag)

    def closest_vert_on_face(face_id, p_obj):
        it_poly.setIndex(face_id)
        vidx = it_poly.getVertices()
        best, best_d2 = None, 1e100
        for v in vidx:
            d2 = _dist2(pts_obj[v], p_obj)   # <- was (pts_obj[v] - p_obj).lengthSq()
            if d2 < best_d2:
                best, best_d2 = v, d2
        return best


    def mirror_world_point(pw):
        if axis == "x": return om.MPoint(2*center - pw.x, pw.y, pw.z)
        if axis == "y": return om.MPoint(pw.x, 2*center - pw.y, pw.z)
        return om.MPoint(pw.x, pw.y, 2*center - pw.z)

    # Build target->source vertex map
    vtx_map = {i: i for i in seam_ids}  # seam copies to itself

    # --- Fast pass: exact-match mirrored positions with a spatial hash.
    # On symmetric meshes this resolves nearly every vertex with pure dict
    # lookups - zero per-vertex API calls.
    cell = max(tol, 1e-9)

    def _cell_key(p):
        return (int(round(p.x / cell)), int(round(p.y / cell)), int(round(p.z / cell)))

    grid = {}
    for i in src_ids + seam_ids:
        grid.setdefault(_cell_key(pts_wld[i]), []).append(i)

    tol2 = tol * tol
    unmatched = []
    for t in tgt_ids:
        mp_w = mirror_world_point(pts_wld[t])
        kx, ky, kz = _cell_key(mp_w)
        best, best_d2 = None, tol2
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for cand in grid.get((kx + dx, ky + dy, kz + dz), ()):
                        d2 = _dist2(pts_wld[cand], mp_w)
                        if d2 < best_d2:
                            best, best_d2 = cand, d2
        if best is not None:
            vtx_map[t] = best
        else:
            unmatched.append(t)

    # --- Slow pass (asymmetric areas only): accelerated closest-point lookup
    # via MMeshIntersector built once, instead of fn_mesh.getClosestPoint per
    # vertex.
    if unmatched:
        try:
            matcher = om.MMeshIntersector()
            matcher.create(dag.node())  # object-space queries
        except Exception:
            matcher = None

        for t in unmatched:
            mp_w = mirror_world_point(pts_wld[t])       # mirror in WORLD
            mp_o = om.MPoint(mp_w) * Minv               # convert to OBJECT

            try:
                if matcher is not None:
                    pom = matcher.getClosestPoint(mp_o)
                    cp_o = om.MPoint(pom.point)
                    face_id = pom.faceIndex if hasattr(pom, "faceIndex") else pom.face
                else:
                    cp_o, face_id = fn_mesh.getClosestPoint(mp_o, om.MSpace.kObject)
                s = closest_vert_on_face(face_id, cp_o)
            except Exception:
                cp_o = mp_o
                s = None

            if s is None:
                best, best_d2 = None, 1e100
                for cand in src_ids:
                    d2 = _dist2(pts_obj[cand], cp_o)
                    if d2 < best_d2:
                        best, best_d2 = cand, d2
                s = best if best is not None else t

            vtx_map[t] = s

    # Write remapped weights: edit a copy of the flat array, then ONE setWeights call
    def _normalize(arr):
        s = sum(arr);  return [a/s for a in arr] if s > 1e-12 else arr

    new_wts = list(flat_wts)
    for t, s in vtx_map.items():
        src_w = list(slice_w(s))
        remapped = [0.0]*inf_count
        for src_i, w in enumerate(src_w):
            remapped[src_to_tgt_inf_idx[src_i]] += w
        remapped = _normalize(remapped)
        new_wts[t*inf_count:(t+1)*inf_count] = remapped

    idx_all = om.MIntArray(range(inf_count))
    fn_skin.setWeights(dag, comp_all, idx_all, om.MDoubleArray(new_wts), True)

    cmds.inViewMessage(amg="Mirror skin (asymmetry-safe) complete ✔", pos="midCenter", fade=True, fst=2000)







def ss_mirrorDefWeightCode(defMirrorNodes, mirroSourceGeo):

    temp_dir = tempfile.gettempdir()
    _pb_start("ssMirrorProgress", len(defMirrorNodes) * 4)

    for defMirrorNode in defMirrorNodes:
        json_path = os.path.join(temp_dir, f"{defMirrorNode}_WeightData.json")
        ss_saveDefWeightsToJson(mirroSourceGeo, defMirrorNode, json_path)

        with open(json_path, "r") as f:
            weight_data = json.load(f)

        weights = weight_data.get("weights", {})

        # Duplicate source
        source_duplicate = cmds.duplicate(mirroSourceGeo, n='sourceDuplicate')[0]

        cmds.select(cl=True)
        base_jnt = cmds.joint(n='Base_Jnt')
        cmds.select(cl=True)
        L_Weight_Jnt = cmds.joint(n='L_Weight_Jnt', p=(1, 0, 0))
        cmds.select(cl=True)
        R_Weight_Jnt = cmds.joint(n='R_Weight_Jnt', p=(-1, 0, 0))

        cmds.select(base_jnt, L_Weight_Jnt, R_Weight_Jnt, source_duplicate)
        source_skin = cmds.skinCluster(ibp=True, tsb=True)[0]

        # Paint base=1-w / L=w / R=0 for every vertex in ONE setWeights call
        # (was: full-mesh skinPercent + one skinPercent per vertex)
        fnS, dagS, compS, wtsS, infcS = ss_getSkinWeights(source_duplicate, source_skin)
        b_idx = _skin_influence_index(fnS, base_jnt)
        l_idx = _skin_influence_index(fnS, L_Weight_Jnt)
        actual_vtx_count = cmds.polyEvaluate(source_duplicate, vertex=True)
        flat = [0.0] * (actual_vtx_count * infcS)
        for i in range(actual_vtx_count):
            w = float(weights.get(str(i), 0.0))
            flat[i * infcS + b_idx] = 1.0 - w
            flat[i * infcS + l_idx] = w
        ss_setSkinWeights(fnS, dagS, compS, infcS, flat, normalize=False)
        _pb_step("ssMirrorProgress")

        ss_mirror_skin_weights(
            mesh=source_duplicate,
            axis="x",               # "x", "y", or "z"
            center=0.0,             # mirror plane position
            side="LtoR",            # "LtoR" or "RtoL" (which side is source)
            tol=1e-4,               # seam tolerance
            do_influence_remap=True,# L<->R remap by name
            left_tags=("L_", ".L"), # any patterns you use
            right_tags=("R_", ".R")
        )

        _pb_step("ssMirrorProgress")

        geo = source_duplicate                     # transform or shape
        src = L_Weight_Jnt
        tgt = base_jnt
        ss_shift_skin_weights(geo, src, tgt, delete_source=False)
        _pb_step("ssMirrorProgress")

        # Save transferred weights to JSON
        defRightNode = defMirrorNode.replace("L_", "R_")

        vtx_count_target = cmds.polyEvaluate(source_duplicate, vertex=True)
        temp_json = os.path.join(temp_dir, f"{defRightNode}_{mirroSourceGeo}_SkinWeightData.json")

        # Read R_Weight_Jnt column for all vertices in ONE getWeights call
        fnS, dagS, compS, wtsS, infcS = ss_getSkinWeights(source_duplicate, source_skin)
        r_idx = _skin_influence_index(fnS, R_Weight_Jnt)
        r_weights = [wtsS[i * infcS + r_idx] for i in range(vtx_count_target)]

        skin_data = {
            "bend_deformer": defRightNode,
            "geometry": mirroSourceGeo,
            "vertex_count": vtx_count_target,
            "weights": {str(i): r_weights[i] for i in range(vtx_count_target)}
        }

        with open(temp_json, "w") as f:
            json.dump(skin_data, f)

        # Apply to the actual right-side deformer in ONE setAttr call
        ss_setDefWeights(defRightNode, mirroSourceGeo, r_weights)
        cmds.delete(base_jnt, L_Weight_Jnt, R_Weight_Jnt, source_duplicate )
        _pb_step("ssMirrorProgress")

    _pb_end("ssMirrorProgress")


# def ss_findNextFreeInputIndex(deformer):
#     """Returns the first unconnected input[i].inputGeometry index on the given deformer."""
#     used = cmds.getAttr(deformer + ".input", multiIndices=True) or []
#     for i in range(max(used + [0]) + 2):
#         if not cmds.listConnections(f"{deformer}.input[{i}].inputGeometry", source=True):
#             return i
#     return None

# def ss_findNextFreeInputIndex(deformer):
#     used = cmds.getAttr(deformer + ".input", multiIndices=True) or []
#     used = sorted(set(used))
#     free = []
#     for i in used:
#         plug = "{}.input[{}].inputGeometry".format(deformer, i)
#         if not cmds.listConnections(plug, source=True, destination=False, plugs=True):
#             free.append(i)
#     # Return only the last free index (or None if none found)
#     return free[-1] if free else None


def ss_findNextFreeInputIndex(deformer):
    used = cmds.getAttr(deformer + ".input", multiIndices=True) or []
    return (used[-1]+1)


def ss_findNextFreeOutputIndex(deformer):
    # Check if the node has an outputGeometry multi-attribute
    if not cmds.objExists(deformer + ".outputGeometry"):
        return 0
    # Use getAttr with multi=True to get all connected indices
    connections = cmds.getAttr(deformer + ".outputGeometry", multiIndices=True)
    if connections is None:
        return 0
    return len(connections)



def ss_extractVertexIndex(vtx_string):
    match = re.search(r'\[(\d+)\]', vtx_string)
    if match:
        return int(match.group(1))
    return None

def ss_transforInputFronObject(deformer_chain):
    geo = cmds.ls(sl=True)
    for obj in geo:
        geo_shape = cmds.listRelatives(obj, s=True, fullPath=True)[0]
        index = ss_findNextFreeInputIndex(deformer_chain[0])

        # Whatever currently feeds the visible shape (skinCluster, another
        # deformer, ...) becomes the input of the new chain.
        src_plugs = cmds.listConnections(geo_shape + ".inMesh", s=True, d=False, p=True) or []

        if src_plugs:
            # Insert the chain BETWEEN the existing history end and the shape,
            # so the deformers become live inputs of the visible shape
            # (previously the chain output was never wired back to inMesh on
            # skinned targets, leaving the deformers as a dead-end branch).
            cmds.connectAttr(src_plugs[0], f"{deformer_chain[0]}.input[{index}].inputGeometry", force=True)
            for i in range(len(deformer_chain) - 1):
                cmds.connectAttr(
                    f"{deformer_chain[i]}.outputGeometry[{index}]",
                    f"{deformer_chain[i + 1]}.input[{index}].inputGeometry",
                    force=True
                )
            cmds.connectAttr(f"{deformer_chain[-1]}.outputGeometry[{index}]", f"{geo_shape}.inMesh", force=True)
        else:
            # No construction history: temp-bind to create an Orig shape, wire
            # the chain, then unbind (Maya repairs the chain input to the Orig).
            cmds.select(cl=True)
            jnt = cmds.joint()
            cmds.select(obj, jnt)
            skinClu = cmds.skinCluster(ibp=True, tsb=True)
            cmds.connectAttr(f"{skinClu[0]}.outputGeometry[0]", f"{deformer_chain[0]}.input[{index}].inputGeometry", force=True)
            for i in range(len(deformer_chain) - 1):
                cmds.connectAttr(f"{deformer_chain[i]}.outputGeometry[{index}]", f"{deformer_chain[i + 1]}.input[{index}].inputGeometry", force=True)
            cmds.skinCluster(obj, edit=True, unbind=True)
            cmds.delete(jnt)

            cmds.connectAttr(f"{deformer_chain[-1]}.outputGeometry[{index}]", f"{geo_shape}.inMesh", force=True)








def ss_saveDefWeightsToJson(source, defNode, json_path):
    if not cmds.objExists(defNode):
        cmds.error(f"Bend deformer '{defNode}' does not exist.")
        return
    geo = source
    vtx_count = cmds.polyEvaluate(geo, vertex=True)

    # ONE batched query instead of a per-vertex cmds.percent loop
    vals = ss_getDefWeights(defNode, geo)

    weight_data = {
        "bend_deformer": defNode,
        "geometry": geo,
        "vertex_count": vtx_count,
        "weights": {str(i): w for i, w in enumerate(vals)}
    }
    with open(json_path, "w") as f:
        json.dump(weight_data, f)

    cmds.select(cl=True)
    print(f"Saved {defNode} weights to: {json_path}")


















# def ss_saveDefWeightsToJson(defNode, json_path):
#     if not cmds.objExists(defNode):
#         cmds.error(f"Bend deformer '{defNode}' does not exist.")
#         return

#     geoShape = cmds.deformer(defNode, query=True, geometry=True)[0]
#     geo = cmds.listRelatives(geoShape, p=True)[0]
#     #deformSets = cmds.listConnections(defNode + '.message', destination=True, type='objectSet')[0]
#     cmds.select(deformSets)
#     vtxDefApply_count = cmds.ls(sl=True, fl=True)

#     vtx_count = cmds.polyEvaluate(geoShape, vertex=True)
#     cmds.select(f"{geo}.vtx[0:{vtx_count}]")
#     mshvrtSel = cmds.ls(sl=True, fl=True)

#     vtxDefEffected_count = []
#     for i in range(len(vtxDefApply_count)):
#         vtxEffected = ss_extractVertexIndex(vtxDefApply_count[i])
#         vtxDefEffected_count.append(vtxEffected)

#     weight_data = {
#         "bend_deformer": defNode,
#         "geometry": geo,
#         "vertex_count": vtx_count,
#         "weights": {}
#     }
#     for i in vtxDefApply_count:
#         if i in mshvrtSel:
#             w = cmds.percent(defNode, i, q=True, v=True)[0]
#             vtxEffected = ss_extractVertexIndex(i)
#             weight_data["weights"][vtxEffected] = w

#     with open(json_path, "w") as f:
#         json.dump(weight_data, f, indent=4)

#     print(f"Saved bend weights to: {json_path}")

# def ss_loadDefWeightsFromJson(defNode, json_path, target_object=None):
#     if not os.path.exists(json_path):
#         cmds.error(f"JSON file not found: {json_path}")
#         return

#     with open(json_path, "r") as f:
#         weight_data = json.load(f)

#     expected_vtx_count = weight_data.get("vertex_count")
#     weights = weight_data.get("weights", {})

#     if target_object:
#         bend_handle, defNode = cmds.nonLinear(target_object, type='bend')
#         geo = target_object
#     else:
#         if not cmds.objExists(defNode):
#             cmds.error(f"Deformer '{defNode}' does not exist.")
#             return
#         geoShape = cmds.deformer(defNode, query=True, geometry=True)
#         if not geoShape:
#             cmds.error(f"No geometry connected to deformer '{defNode}'.")
#             return
#         geo = cmds.listRelatives(geoShape, p=True)[0]

#     actual_vtx_count = cmds.polyEvaluate(geo, vertex=True)

#     if actual_vtx_count != expected_vtx_count:
#         cmds.error(f"Vertex count mismatch! Expected {expected_vtx_count}, found {actual_vtx_count} on geometry '{geo}'.")
#         return

#     for idx_str, weight in weights.items():
#         idx = int(idx_str)
#         cmds.percent(defNode, f"{geo}.vtx[{idx}]", v=weight)

#     print(f"Applied weights from JSON to bend deformer '{defNode}' on object '{geo}'.")





def ss_loadDefWeightsFromJson(defNode, json_path, target_object=None):
    """
    Loads bend deformer weights from a JSON file and applies them to the specified deformer.
    If target_object is provided, it will apply a new bend deformer to the object and load weights into it.
    """
    if not cmds.objExists(defNode):
        cmds.error(f"deformer '{defNode}' does not exist.")
        return

    if not os.path.exists(json_path):
        cmds.error(f"JSON file not found: {json_path}")
        return

    with open(json_path, "r") as f:
        weight_data = json.load(f)

    expected_vtx_count = weight_data.get("vertex_count")
    weights = weight_data.get("weights", {})

    # Verify connected geometry
    geoShape = cmds.deformer(defNode, query=True, geometry=True)

    if target_object:
        # find geo or mesh of the deformer
        geoShape = cmds.deformer(defNode, query=True, geometry=True)
        if not geoShape:
            cmds.error(f"No geometry connected to bend deformer '{defNode}'.")
        geo = []
        geos = cmds.listRelatives(geoShape, p=True)
        if target_object in geos:
            geo.append(target_object)
            print(f"{target_object} is in the scene.")
        else:
            print(f"{target_object} is not in the scene.")
            return
        geo = geo[0]

    actual_vtx_count = cmds.polyEvaluate(geo, vertex=True)

    if actual_vtx_count != expected_vtx_count:
        cmds.error(f"Vertex count mismatch! Expected {expected_vtx_count}, found {actual_vtx_count} on geometry '{geo}'.")
        return

    # Apply ALL weights in one call instead of per-vertex cmds.percent
    ss_setDefWeights(defNode, geo, weights)

    print(f"Applied weights from JSON to bend deformer '{defNode}' on object '{geo}'.")

# Example usage:










def ss_copyDefWeightFromOtherCode(defNode, source_object, target_objects):
    temp_dir = tempfile.gettempdir()
    _pb_start("ssDefWeightProgress", len(defNode) * len(target_objects))

    # Add deformer input connection once per target
    for target_object in target_objects:
        cmds.select(target_object)
        ss_transforInputFronObject(defNode)

    for defNo in defNode:
        json_path = os.path.join(temp_dir, f"{defNo}_WeightData.json")
        ss_saveDefWeightsToJson(source_object, defNo, json_path)

        with open(json_path, "r") as f:
            weight_data = json.load(f)

        weights = weight_data.get("weights", {})

        # Duplicate source and bind it ONCE per deformer (was once per deformer x target)
        source_duplicate = cmds.duplicate(source_object, n='sourceDuplicate')[0]

        cmds.select(cl=True)
        base_jnt = cmds.joint(n='Base_Jnt')
        cmds.select(cl=True)
        weight_jnt = cmds.joint(n='Weight_Jnt', p=(1, 0, 0))

        cmds.select(base_jnt, weight_jnt, source_duplicate)
        source_skin = cmds.skinCluster(ibp=True, tsb=True)[0]

        # Paint base=1-w / weight_jnt=w for every vertex in ONE setWeights call
        # (was: full-mesh skinPercent + one skinPercent per vertex)
        fnS, dagS, compS, wtsS, infcS = ss_getSkinWeights(source_duplicate, source_skin)
        b_idx = _skin_influence_index(fnS, base_jnt)
        w_idx = _skin_influence_index(fnS, weight_jnt)
        n_src = cmds.polyEvaluate(source_duplicate, vertex=True)
        flat = [0.0] * (n_src * infcS)
        for i in range(n_src):
            w = float(weights.get(str(i), 0.0))
            flat[i * infcS + b_idx] = 1.0 - w
            flat[i * infcS + w_idx] = w
        ss_setSkinWeights(fnS, dagS, compS, infcS, flat, normalize=False)

        for target_object in target_objects:
            target_duplicate = cmds.duplicate(target_object, n=f"{target_object}_Duplicate")[0]

            # Bind duplicate with same joints and transfer weights
            cmds.select(base_jnt, weight_jnt, target_duplicate)
            target_skin = cmds.skinCluster(ibp=True, tsb=True)[0]

            cmds.copySkinWeights(
                sourceSkin=source_skin,
                destinationSkin=target_skin,
                noMirror=True,
                surfaceAssociation='closestPoint',
                influenceAssociation='oneToOne'
            )

            # Read weight_jnt column for all vertices in ONE getWeights call
            fnT, dagT, compT, wtsT, infcT = ss_getSkinWeights(target_duplicate, target_skin)
            wj_idx = _skin_influence_index(fnT, weight_jnt)
            vtx_count_target = cmds.polyEvaluate(target_duplicate, vertex=True)
            target_weights = [wtsT[i * infcT + wj_idx] for i in range(vtx_count_target)]

            # Save transferred weights to JSON
            temp_json = os.path.join(temp_dir, f"{defNo}_{target_object}_SkinWeightData.json")
            skin_data = {
                "bend_deformer": defNo,
                "geometry": target_object,
                "vertex_count": vtx_count_target,
                "weights": {str(i): target_weights[i] for i in range(vtx_count_target)}
            }
            with open(temp_json, "w") as f:
                json.dump(skin_data, f)

            # Apply to the actual target deformer in ONE setAttr call
            ss_setDefWeights(defNo, target_object, target_weights)

            print(f"Applied weights from JSON to deformer '{defNo}' on object '{target_object}'.")

            # Clean up target duplicate and skin
            cmds.skinCluster(target_skin, edit=True, unbind=True)
            cmds.delete(target_duplicate)
            _pb_step("ssDefWeightProgress")

        # Final cleanup
        cmds.skinCluster(source_skin, edit=True, unbind=True)
        cmds.delete(source_duplicate, base_jnt, weight_jnt)

    _pb_end("ssDefWeightProgress")


# ---------- progress bar helpers ----------

def _pb_start(bar, max_value):
    """Reset a tab progress bar; safe no-op if the UI is not open."""
    if cmds.progressBar(bar, exists=True):
        cmds.progressBar(bar, e=True, maxValue=max(1, int(max_value)), progress=0)


def _pb_step(bar, n=1):
    if cmds.progressBar(bar, exists=True):
        cmds.progressBar(bar, e=True, step=n)


def _pb_end(bar):
    if cmds.progressBar(bar, exists=True):
        cmds.progressBar(bar, e=True, progress=cmds.progressBar(bar, q=True, maxValue=True))


# --- UI Launcher ---
# ... [All your existing function definitions remain unchanged above] ...

def ss_defWeightToolUI():
    if cmds.window("ssDefWeightWin", exists=True):
        cmds.deleteUI("ssDefWeightWin")

    win = cmds.window("ssDefWeightWin", title="Deformer Weight Tool v1.0", sizeable=True, widthHeight=(400, 400))
    tabs = cmds.tabLayout(innerMarginWidth=5, innerMarginHeight=5)

    # ---------------------- Weight Tools Tab ----------------------
    weightCol = cmds.columnLayout(adjustableColumn=True, rowSpacing=10, columnAlign="center")
    cmds.text(label="Deformer Weight IO")

    cmds.textScrollList("defNodeList", numberOfRows=6, allowMultiSelection=True, append=[], height=80)
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnAlign=(1, 'center'),
                   columnAttach=[(1, 'both', 2), (2, 'both', 2)], columnWidth=[(1, 190), (2, 190)])
    cmds.button(label="<< Add Selected Deformers",
        command=lambda _: [cmds.textScrollList("defNodeList", e=True, append=item)
                           for item in cmds.ls(sl=True)
                           if item not in (cmds.textScrollList("defNodeList", q=True, ai=True) or [])])
    cmds.button(label="Clear Deformer List",
        command=lambda _: cmds.textScrollList("defNodeList", e=True, removeAll=True))
    cmds.setParent("..")

    cmds.textFieldButtonGrp("sourceObjField", label="Source Obj:", buttonLabel="<<", cw3=[60, 140, 40],
                            buttonCommand=lambda: cmds.textFieldButtonGrp("sourceObjField", e=True, text=cmds.ls(sl=True)[0] if cmds.ls(sl=True) else ""))
    cmds.textScrollList("targetObjList", numberOfRows=8, allowMultiSelection=True, append=[], height=100)
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnAlign=(1, 'center'),
                   columnAttach=[(1, 'both', 2), (2, 'both', 2)], columnWidth=[(1, 190), (2, 190)])
    cmds.button(label="<< Add Selected to Target List",
        command=lambda _: [cmds.textScrollList("targetObjList", e=True, append=item)
                           for item in cmds.ls(sl=True)
                           if item not in (cmds.textScrollList("targetObjList", q=True, ai=True) or [])])
    cmds.button(label="Clear Target List",
        command=lambda _: cmds.textScrollList("targetObjList", e=True, removeAll=True))
    cmds.setParent("..")

    def get_selected_deformers():
        return cmds.textScrollList("defNodeList", q=True, ai=True) or []

    def save_weights_ui():
        defNodes = get_selected_deformers()
        source = cmds.textFieldButtonGrp("sourceObjField", q=True, text=True)
        json_path = cmds.fileDialog2(fileMode=0, caption="Save Deformer Weights", fileFilter="*.json")
        if json_path:
            _pb_start("ssDefWeightProgress", len(defNodes))
            for defNode in defNodes:
                ss_saveDefWeightsToJson(source, defNode, json_path[0])
                _pb_step("ssDefWeightProgress")
            _pb_end("ssDefWeightProgress")

    def load_weights_ui():
        defNodes = get_selected_deformers()
        target = cmds.textScrollList("targetObjList", q=True, ai=True)
        json_path = cmds.fileDialog2(fileMode=1, caption="Load Deformer Weights", fileFilter="*.json")
        if json_path:
            _pb_start("ssDefWeightProgress", len(target) * len(defNodes))
            for tgt in target:
                for defNode in defNodes:
                    ss_loadDefWeightsFromJson(defNode, json_path[0], target_object=tgt)
                    _pb_step("ssDefWeightProgress")
            _pb_end("ssDefWeightProgress")

    def copy_weights_ui():
        defNodes = get_selected_deformers()
        source = cmds.textFieldButtonGrp("sourceObjField", q=True, text=True)
        targets = cmds.textScrollList("targetObjList", q=True, ai=True)
        if defNodes and source and targets:
            ss_copyDefWeightFromOtherCode(defNodes, source, targets)

    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnAlign=(1, 'center'),
                   columnAttach=[(1, 'both', 2), (2, 'both', 2)], columnWidth=[(1, 190), (2, 190)])
    cmds.button(label="Save Weights to JSON", command=lambda _: save_weights_ui())
    cmds.button(label="Load Weights from JSON", command=lambda _: load_weights_ui())
    cmds.setParent("..")
    cmds.button(label="Copy Weights to Targets", height=30, command=lambda _: copy_weights_ui())
    cmds.progressBar("ssDefWeightProgress", height=14)
    cmds.setParent("..")  # End of first columnLayout
    # ---------------------- Mirror Tools Tab ----------------------
    mirrorCol = cmds.columnLayout(adjustableColumn=True, rowSpacing=10, columnAlign="center")
    cmds.text(label="Mirror Deformer Weights")

    def mirror_weights_ui():
        defMirrorNodes = cmds.textScrollList("mirrorList", q=True, ai=True) or []
        mirroSourceGeo = cmds.textFieldButtonGrp("mirrorSourceObjField", q=True, text=True)
        if defMirrorNodes and mirroSourceGeo:
            ss_mirrorDefWeightCode(defMirrorNodes, mirroSourceGeo)

    cmds.button(label="<< Add Selected to Mirror List",
        command=lambda _: [cmds.textScrollList("mirrorList", e=True, append=item)
                        for item in cmds.ls(sl=True)
                        if item not in (cmds.textScrollList("mirrorList", q=True, ai=True) or [])])

    cmds.button(label="Clear Mirror List",
        command=lambda _: cmds.textScrollList("mirrorList", e=True, removeAll=True))

    cmds.textScrollList("mirrorList", numberOfRows=8, allowMultiSelection=True, append=[], height=100)

    cmds.textFieldButtonGrp("mirrorSourceObjField", label="Mirror Source Obj: ", buttonLabel="<<", cw3=[120, 140, 40],
                            buttonCommand=lambda: cmds.textFieldButtonGrp("mirrorSourceObjField", e=True, text=cmds.ls(sl=True)[0] if cmds.ls(sl=True) else ""))

    cmds.button(label="Mirror Deformer Weights", command=lambda _:  mirror_weights_ui())
    cmds.progressBar("ssMirrorProgress", height=14)
    cmds.setParent("..")  # End of second columnLayout


    cmds.tabLayout(tabs, edit=True, tabLabel=[
    (weightCol, "Deformer Weights"),
    (mirrorCol, "Mirror Weights"),
])

    cmds.showWindow(win)

# --- Helpers ---

def add_selected_to_BlendShape_list(scroll_list_name):
    selected = cmds.ls(selection=True)
    if not selected:
        cmds.warning("No objects selected.")
        return
    attr_sel = cmds.channelBox('mainChannelBox', q=True, selectedMainAttributes=True)
    obj_sel = cmds.ls(sl=True)

    if not obj_sel or not attr_sel:
        cmds.warning("Select a blendShape node and a target attribute (e.g., smile).")
        return

    blendshape_node = obj_sel[0]
    existing = cmds.textScrollList(scroll_list_name, query=True, allItems=True) or []

    # Add every highlighted channel-box attribute (e.g. 'smile', 'frown', ...)
    for selected_attr in attr_sel:
        selBS = f"{blendshape_node}.{selected_attr}"
        if selBS not in existing:
            cmds.textScrollList(scroll_list_name, edit=True, append=selBS)
            existing.append(selBS)

def add_selected_to_list(scroll_list_name):
    selected = cmds.ls(selection=True)
    if not selected:
        cmds.warning("No objects selected.")
        return
    existing = cmds.textScrollList(scroll_list_name, query=True, allItems=True) or []
    for sel in selected:
        if sel not in existing:
            cmds.textScrollList(scroll_list_name, edit=True, append=sel)

def set_mirror_source():
    sel = cmds.ls(selection=True)
    if sel:
        cmds.textField("mirrorSourceField", edit=True, text=sel[0])
    else:
        cmds.warning("Select one object to set as mirror source.")


#ss_defWeightToolUI()






