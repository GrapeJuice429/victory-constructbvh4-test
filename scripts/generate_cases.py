"""Generate the four synthetic 16 KiB constructor inputs and scalar reference outputs.

The scene is two triangles and one to three instances separated along x. The
reference covers the shallow binary trees used by these cases. All files contain
little-endian words and guest addresses are byte offsets within a single array.
"""
import argparse
from pathlib import Path
import struct


HEAP_BYTES = 16 * 1024
ROOT_TABLE = 0
INIT_RECORD = 0x300
INSTANCE_DESCRIPTORS = 0x800
RESULT = 0x1000
PARENTS_OFFSET = 64
ELEMENTS_OFFSET = 128
NODE_BYTES = 128
SCRATCH = (0x2000, 0x2100, 0x2200, 0x2300, 0x2400, 0x2500)
KEY_PAIRS = 0x2600
INDEX_PAIRS = 0x2700
BOTTOM_TREE = 0x3000

INVALID = 0xFFFFFFFF
BOX32 = 5
INSTANCE = 6
MAX_FLOAT = float.fromhex("0x1.fffffep127")
CASES = ((2, 0), (1, 1), (2, 2), (3, 3))


def put_words(data, offset, *values):
    struct.pack_into(f"<{len(values)}I", data, offset, *values)


def put_floats(data, offset, *values):
    struct.pack_into(f"<{len(values)}f", data, offset, *values)


def get_word(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def box_union(boxes):
    return tuple(min(box[i] for box in boxes) for i in range(3)) + tuple(
        max(box[i] for box in boxes) for i in range(3, 6))


def vertex_bounds(points):
    return tuple(min(v[i] for v in points) for i in range(3)) + tuple(
        max(v[i] for v in points) for i in range(3))


def node_id(relative_offset, kind):
    # The low three bits identify the node, the upper bits address a 64-byte block.
    assert relative_offset % 64 == 0 and 0 <= kind < 8
    return (relative_offset // 64) * 8 + kind


def write_box_node(data, offset, children, missing_bound):
    """Each of four slots has a node ID and a float32 min/max box."""
    assert len(children) == 4
    for i, child in enumerate(children):
        ref, box = child if child is not None else (
            INVALID, (missing_bound,) * 3 + (-missing_bound,) * 3)
        put_words(data, offset + 4 * i, ref)
        put_floats(data, offset + 16 + 24 * i, *box)


def write_bottom_tree(data):
    # Each triangle has three corners, represented by a four-vertex leaf whose
    # final corner repeats. The two z planes make the bounds easy to verify.
    triangles = (
        ((-1, -1, 2), (1, -1, 2), (-1, 1, 2)),
        ((-1, 1, 3), (1, -1, 3), (1, 1, 3)),
    )
    bounds = [vertex_bounds(triangle) for triangle in triangles]
    root_offset = ELEMENTS_OFFSET + 64 * len(triangles)
    root = node_id(root_offset, BOX32)
    put_words(data, BOTTOM_TREE, 0x80000006, 5, 0, len(triangles))
    put_floats(data, BOTTOM_TREE + 16, *box_union(bounds))
    put_words(data, BOTTOM_TREE + 40, root_offset, 0, root, ELEMENTS_OFFSET, PARENTS_OFFSET, 0)
    put_words(data, BOTTOM_TREE + PARENTS_OFFSET, root, root, INVALID, INVALID)
    children = []
    for i, (triangle, box) in enumerate(zip(triangles, bounds)):
        relative = ELEMENTS_OFFSET + 64 * i
        leaf = BOTTOM_TREE + relative
        for j, vertex in enumerate((*triangle, triangle[-1])):
            put_floats(data, leaf + 12 * j, *vertex)
        put_words(data, leaf + 48, 0x701, i, INVALID, 9)
        children.append((node_id(relative, 0), box))
    write_box_node(data, BOTTOM_TREE + root_offset, [*children, None, None], float("inf"))
    return root, box_union(bounds)


def write_instance(data, i, bottom_root, bottom_box):
    tx = float(i * 4)
    matrix = (1, 0, 0, tx, 0, 1, 0, 0, 0, 0, 1, 0)
    masked_id = (0xFF << 24) | (17 + i)

    descriptor = INSTANCE_DESCRIPTORS + 64 * i
    put_floats(data, descriptor, *matrix)
    put_words(data, descriptor + 48, masked_id, 0, BOTTOM_TREE, 0)

    leaf = RESULT + ELEMENTS_OFFSET + NODE_BYTES * i
    put_floats(data, leaf, *matrix)
    put_words(data, leaf + 48, masked_id, 0, 0x80000000 | bottom_root, BOTTOM_TREE >> 8)
    # Negated translations preserve the sign bit of zero in all three coordinates.
    put_floats(data, leaf + 64, -tx, -0.0, -0.0, 1, 0, 0, 0, 1, 0, 0, 0, 1)
    put_words(data, leaf + 112, i)
    x0, y0, z0, x1, y1, z1 = bottom_box
    return (x0 + tx, y0, z0, x1 + tx, y1, z1)


def make_input(count, initial):
    if (count, initial) not in CASES:
        raise ValueError("Supported fixtures are (2, 0), (1, 1), (2, 2) and (3, 3)")
    data = bytearray(HEAP_BYTES)
    counters, links, edges, allocation, boxes, indices = SCRATCH
    roots = (RESULT, counters, links, edges, RESULT + PARENTS_OFFSET, allocation, boxes, indices)
    struct.pack_into("<8Q4I", data, ROOT_TABLE, *roots, 8, 0, 0, 0)
    bottom_root, bottom_box = write_bottom_tree(data)
    put_words(data, INIT_RECORD, RESULT, 0, 0, 0, 6, bottom_root, initial, INVALID,
              ELEMENTS_OFFSET, PARENTS_OFFSET)
    leaves = [write_instance(data, i, bottom_root, bottom_box) for i in range(count)]
    put_words(data, RESULT, 6, 0, bottom_root, initial)
    put_floats(data, RESULT + 16, *box_union([(0,) * 6, *leaves]))
    put_words(data, RESULT + 40, 0, INVALID, INVALID, ELEMENTS_OFFSET, PARENTS_OFFSET, 0x007F7F7F)

    first_leaf = max(1, count - 1)
    for i, box in enumerate(leaves):
        put_floats(data, boxes + 24 * (first_leaf + i), *box)
        put_words(data, KEY_PAIRS + 8 * i, i + 1, 0)
        put_words(data, INDEX_PAIRS + 8 * i, i, 0x80000000)
    if initial == 1:
        put_words(data, edges, first_leaf, INVALID, INVALID, INVALID)
    elif initial >= 2:
        # A right-growing binary tree. Each internal node names a leaf on
        # its left and either the next internal node or the last leaf on its right.
        for parent in range(first_leaf):
            left = first_leaf + parent
            right = parent + 1 if parent + 1 < first_leaf else left + 1
            put_words(data, edges + 8 * parent, left, right)
            put_words(data, links + 4 * left, parent)
            put_words(data, links + 4 * right, parent)
    return data


def construct(source):
    """Scalar reference for zero to three active instances in the shallow fixture trees."""
    data = bytearray(source)
    result, counters, links, edges, parents, allocation, boxes, _indices = struct.unpack_from("<8Q", data)
    count = get_word(data, result + 12)
    if count == 0:
        return data
    if count > 3:
        raise ValueError("The scalar model supports at most three active instances")
    first_leaf = max(1, count - 1)
    element_offset = get_word(data, result + 52)
    root_offset = element_offset + count * NODE_BYTES
    root = node_id(root_offset, BOX32)

    def leaf(node):
        relative = element_offset + (node - first_leaf) * NODE_BYTES
        return node_id(relative, INSTANCE), struct.unpack_from("<6f", data, boxes + 24 * node)

    def binary_children(node):
        return struct.unpack_from("<2I", data, edges + 8 * node)

    # Collapse one binary level into four physical child slots. A direct leaf
    # occupies the first slot of its pair, keeping the second slot inactive.
    children = []
    for child in binary_children(0):
        if child == INVALID:
            children.extend((None, None))
        elif child >= first_leaf:
            children.extend((leaf(child), None))
        else:
            children.extend(leaf(n) if n != INVALID else None for n in binary_children(child))

    write_box_node(data, result + root_offset, children, MAX_FLOAT)
    put_words(data, result + 4, (root_offset + NODE_BYTES) // 64 - 1)
    put_words(data, result + 40, root_offset)
    put_words(data, result + 48, root)
    put_words(data, allocation, NODE_BYTES)
    put_words(data, parents + 8 * count, INVALID)
    for i in range(count):
        node = first_leaf + i
        put_words(data, parents + 8 * i, root)
        put_words(data, result + element_offset + NODE_BYTES * i + 116, root)
        put_words(data, links + 4 * node, leaf(node)[0])
        put_words(data, edges + 8 * node, INVALID, INVALID)

    def reduce_bounds(node):
        if node >= first_leaf:
            return 1, leaf(node)[1]
        parts = [reduce_bounds(child) for child in binary_children(node) if child != INVALID]
        descendants = sum(n for n, _ in parts)
        combined = box_union([b for _, b in parts])
        put_words(data, counters + 4 * node, max(2, descendants))
        put_floats(data, boxes + 24 * node, *combined)
        return descendants, combined

    reduce_bounds(0)
    return data


def check_case(source, expected, count, initial):
    assert len(source) == len(expected) == HEAP_BYTES
    assert source[BOTTOM_TREE:] == expected[BOTTOM_TREE:]
    if not initial:
        assert expected == source
        return
    root_offset = get_word(expected, RESULT + 40)
    assert root_offset + NODE_BYTES == (384, 512, 640)[count - 1]
    child_ids = struct.unpack_from("<4I", expected, RESULT + root_offset)
    assert [v for v in child_ids if v != INVALID] == [
        node_id(ELEMENTS_OFFSET + NODE_BYTES * i, INSTANCE) for i in range(count)]
    assert struct.unpack_from("<6f", expected, SCRATCH[4]) == (-1, -1, 2, 4 * (count - 1) + 1, 1, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for count, initial in CASES:
        source = make_input(count, initial)
        expected = construct(source)
        check_case(source, expected, count, initial)
        stem = f"instances-{count}-initial-{initial}"
        (args.output_directory / f"{stem}.input").write_bytes(source)
        (args.output_directory / f"{stem}.expected").write_bytes(expected)


if __name__ == "__main__":
    main()
