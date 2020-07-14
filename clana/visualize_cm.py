#!/usr/bin/env python

"""
Optimize confusion matrix.

For more information, see

* http://cs.stackexchange.com/q/70627/2914
* http://datascience.stackexchange.com/q/17079/8820
"""

# Core Library
import json
import logging
import random
from typing import Any, Callable, Dict, List, Optional, Tuple

# Third party
import numpy as np
from jinja2 import Template
from mpl_toolkits.axes_grid1 import make_axes_locatable
from pkg_resources import resource_filename
from sklearn.metrics import silhouette_score

# First party
import clana.clustering
import clana.cm_metrics
import clana.io
import clana.utils

cfg = clana.utils.load_cfg()
logger = logging.getLogger(__name__)


def main(
    cm_file: str,
    perm_file: str,
    steps: int,
    labels_file: str,
    zero_diagonal: bool,
    limit_classes: Optional[int] = None,
    output: Optional[str] = None,
) -> None:
    """
    Run optimization and generate output.

    Parameters
    ----------
    cm_file : str
    perm_file : str
    steps : int
    labels_file : str
    zero_diagonal : bool
    limit_classes : int, optional (default: no limit)
    output : str
    """
    cm = clana.io.read_confusion_matrix(cm_file)
    perm = clana.io.read_permutation(cm_file, perm_file)
    labels = clana.io.read_labels(labels_file, len(cm))
    n, m = cm.shape
    if n != m:
        raise ValueError(
            f"Confusion matrix is expected to be square, but was {n} x {m}"
        )
    if len(labels) - 1 != n:
        print(
            "Confusion matrix is {n} x {n}, but len(labels)={nb_labels}".format(
                n=n, nb_labels=len(labels)
            )
        )

    cm_orig = cm.copy()

    get_cm_problems(cm, labels)

    weights = calculate_weight_matrix(len(cm))
    print("Score: {}".format(calculate_score(cm, weights)))
    result = simulated_annealing(
        cm, perm, score=calculate_score, deterministic=True, steps=steps
    )
    print("Score: {}".format(calculate_score(result["cm"], weights)))
    print("Perm: {}".format(list(result["perm"])))
    clana.io.ClanaCfg.store_permutation(cm_file, result["perm"], steps)
    labels = [labels[i] for i in result["perm"]]
    class_indices = list(range(len(labels)))
    class_indices = [class_indices[i] for i in result["perm"]]
    logger.info(f"Classes: {labels}")
    acc = clana.cm_metrics.get_accuracy(cm_orig)
    print("Accuracy: {:0.2f}%".format(acc * 100))
    start = 0
    if limit_classes is None:
        limit_classes = len(cm)
    if output is None:
        output = cfg["visualize"]["save_path"]
    plot_cm(
        result["cm"][start:limit_classes, start:limit_classes],
        zero_diagonal=zero_diagonal,
        labels=labels[start:limit_classes],
        output=output,
    )
    create_html_cm(
        result["cm"][start:limit_classes, start:limit_classes],
        zero_diagonal=zero_diagonal,
        labels=labels[start:limit_classes],
    )
    if len(cm) < 5:
        print(
            "You only have {} classes. Clustering for less than 5 classes "
            "should be done manually.".format(len(cm))
        )
        return
    grouping = clana.clustering.extract_clusters(result["cm"], labels)
    y_pred = [0]
    cluster_i = 0
    for el in grouping:
        if el:
            cluster_i += 1
        y_pred.append(cluster_i)
    logger.info("silhouette_score={}".format(silhouette_score(cm, y_pred)))
    # Store grouping as hierarchy
    with open(cfg["visualize"]["hierarchy_path"], "w") as outfile:
        hierarchy = clana.clustering.apply_grouping(class_indices, grouping)
        hierarchy = clana.clustering._remove_single_element_groups(hierarchy)
        str_ = json.dumps(
            hierarchy,
            indent=4,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        outfile.write(str_)

    # Print nice
    for group in clana.clustering.apply_grouping(labels, grouping):
        print("\t{}: {}".format(len(group), list(group)))


def get_cm_problems(cm: np.ndarray, labels: List[str]) -> None:
    """
    Find problems of a classifier by analzing its confusion matrix.

    Parameters
    ----------
    cm : ndarray
    labels : List[str]
    """
    n = len(cm)

    # Find classes which are not present in the dataset
    for i in range(n):
        if sum(cm[i]) == 0:
            logger.warning("The class '{}' was not in the dataset.".format(labels[i]))

    # Find classes which are never predicted
    cm = cm.transpose()
    never_predicted = []
    for i in range(n):
        if sum(cm[i]) == 0:
            never_predicted.append(labels[i])
    if len(never_predicted) > 0:
        logger.warning(f"The following classes were never predicted: {never_predicted}")


def calculate_score(cm: np.ndarray, weights: float) -> int:
    """
    Calculate a score how close big elements of cm are to the diagonal.

    Examples
    --------
    >>> cm = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
    >>> weights = calculate_weight_matrix(3)
    >>> weights.shape
    (3, 3)
    >>> calculate_score(cm, weights)
    32
    """
    return int(np.tensordot(cm, weights, axes=((0, 1), (0, 1))))


def calculate_weight_matrix(n: int) -> np.ndarray:
    """
    Calculate the weights for each position.

    The weight is the distance to the diagonal.

    Parameters
    ----------
    n : int

    Examples
    --------
    >>> calculate_weight_matrix(3)
    array([[0.  , 1.01, 2.02],
           [1.01, 0.  , 1.03],
           [2.02, 1.03, 0.  ]])
    """
    weights = np.abs(np.arange(n) - np.arange(n)[:, None])
    weights = np.array(weights, dtype=np.float)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            weights[i][j] += (i + j) * 0.01
    return weights


def swap(cm: np.ndarray, i: int, j: int) -> np.ndarray:
    """
    Swap row and column i and j in-place.

    Parameters
    ----------
    cm : np.ndarray
    i : int
    j : int

    Returns
    -------
    cm : np.ndarray

    Examples
    --------
    >>> cm = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
    >>> swap(cm, 2, 0)
    array([[8, 7, 6],
           [5, 4, 3],
           [2, 1, 0]])
    """
    # swap columns
    copy = cm[:, i].copy()
    cm[:, i] = cm[:, j]
    cm[:, j] = copy
    # swap rows
    copy = cm[i, :].copy()
    cm[i, :] = cm[j, :]
    cm[j, :] = copy
    return cm


def move_1d(
    perm: np.ndarray, from_start: int, from_end: int, insert_pos: int
) -> np.ndarray:
    """
    Move a block in a list.

    Parameters
    ----------
    perm : np.ndarray
        Permutation
    from_start : int
    from_end : int
    insert_pos : int

    Returns
    -------
    perm : np.ndarray
        The new permutation
    """
    if not (insert_pos < from_start or insert_pos > from_end):
        raise ValueError(
            "insert_pos={} needs to be smaller than from_start={}"
            " or greater than from_end={}".format(insert_pos, from_start, from_end)
        )
    if insert_pos > from_end:
        p_new = list(range(from_end + 1, insert_pos + 1)) + list(
            range(from_start, from_end + 1)
        )
    else:
        p_new = list(range(from_start, from_end + 1)) + list(
            range(insert_pos, from_start)
        )
    p_old = sorted(p_new)
    perm[p_old] = perm[p_new]
    return perm


def move(cm: np.ndarray, from_start: int, from_end: int, insert_pos: int) -> np.ndarray:
    """
    Move rows from_start - from_end to insert_pos in-place.

    Parameters
    ----------
    cm : np.ndarray
    from_start : int
    from_end : int
    insert_pos : int

    Returns
    -------
    cm : np.ndarray

    Examples
    --------
    >>> cm = np.array([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 0, 1], [2, 3, 4, 5]])
    >>> move(cm, 1, 2, 0)
    array([[5, 6, 4, 7],
           [9, 0, 8, 1],
           [1, 2, 0, 3],
           [3, 4, 2, 5]])
    """
    if not (insert_pos < from_start or insert_pos > from_end):
        raise ValueError(
            "insert_pos={} needs to be smaller than from_start={}"
            " or greater than from_end={}".format(insert_pos, from_start, from_end)
        )
    if insert_pos > from_end:
        p_new = list(range(from_end + 1, insert_pos + 1)) + list(
            range(from_start, from_end + 1)
        )
    else:
        p_new = list(range(from_start, from_end + 1)) + list(
            range(insert_pos, from_start)
        )
    p_old = sorted(p_new)
    # swap columns
    cm[:, p_old] = cm[:, p_new]
    # swap rows
    cm[p_old, :] = cm[p_new, :]
    return cm


def swap_1d(perm: np.ndarray, i: int, j: int) -> np.ndarray:
    """
    Swap two elements of a 1-D numpy array in-place.

    Parameters
    ----------
    parm : np.ndarray
    i : int
    j : int

    Examples
    --------
    >>> perm = np.array([2, 1, 2, 3, 4, 5, 6])
    >>> swap_1d(perm, 2, 6)
    array([2, 1, 6, 3, 4, 5, 2])
    """
    perm[i], perm[j] = perm[j], perm[i]
    return perm


def apply_permutation(cm: np.ndarray, perm: List[int]) -> np.ndarray:
    """
    Apply permutation to a matrix.

    Parameters
    ----------
    cm : ndarray
    perm : List[int]

    Examples
    --------
    >>> cm = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
    >>> perm = np.array([2, 0, 1])
    >>> apply_permutation(cm, perm)
    array([[8, 6, 7],
           [2, 0, 1],
           [5, 3, 4]])
    """
    return cm[perm].transpose()[perm].transpose()


def simulated_annealing(
    current_cm: np.ndarray,
    current_perm: Optional[np.ndarray] = None,
    score: Callable = calculate_score,
    steps: int = 2 * 10 ** 5,
    temp: float = 100.0,
    cooling_factor: float = 0.99,
    deterministic: bool = False,
) -> Dict[str, Any]:
    """
    Optimize current_cm by randomly swapping elements.

    Parameters
    ----------
    current_cm : np.ndarray
    current_perm : None or iterable, optional (default: None)
    steps : int, optional (default: 2 * 10**4)
    temp : float > 0.0, optional (default: 100.0)
        Temperature
    cooling_factor: float in (0, 1), optional (default: 0.99)

    Returns
    -------
    best_result : Dict[str, Any]
        "best_cm"
        "best_perm"
    """
    if temp <= 0.0:
        raise ValueError(f"temp={temp} needs to be positive")
    if cooling_factor <= 0.0 or cooling_factor >= 1.0:
        raise ValueError(
            "cooling_factor={} needs to be in the interval "
            "(0, 1)".format(cooling_factor)
        )
    n = len(current_cm)
    logger.info(f"n={n}")

    # Load the initial permutation
    if current_perm is None:
        current_perm = list(range(n))
    current_perm = np.array(current_perm)

    # Pre-calculate weights
    weights = calculate_weight_matrix(n)

    # Apply the permutation
    current_cm = apply_permutation(current_cm, current_perm)
    current_score = score(current_cm, weights)

    best_cm = current_cm
    best_score = current_score
    best_perm = current_perm

    logger.info(f"## Starting Score: {current_score:0.2f}")
    for step in range(steps):
        tmp_cm = np.array(current_cm, copy=True)
        perm, make_swap = generate_permutation(n, current_perm, tmp_cm)
        tmp_score = score(tmp_cm, weights)

        # Should be swapped?
        if deterministic:
            chance = 1.0
        else:
            chance = random.random()
            temp *= 0.99
        hot_prob_thresh = min(1, np.exp(-(tmp_score - current_score) / temp))
        if chance <= hot_prob_thresh:
            changed = False
            if best_score > tmp_score:  # minimize
                best_perm = perm
                best_cm = tmp_cm
                best_score = tmp_score
                changed = True
            current_score = tmp_score
            current_cm = tmp_cm
            current_perm = perm
            if changed:
                logger.info(
                    (
                        "Current: %0.2f (best: %0.2f, "
                        "hot_prob_thresh=%0.4f%%, step=%i, swap=%s)"
                    ),
                    current_score,
                    best_score,
                    (hot_prob_thresh * 100),
                    step,
                    str(make_swap),
                )
    return {"cm": best_cm, "perm": best_perm}


def generate_permutation(
    n: int, current_perm: List[int], tmp_cm: np.ndarray
) -> Tuple[List[int], bool]:
    """
    Generate a new permutation.

    Parameters
    ----------
    n : int
    current_perm : List[int]
    tmp_cm : np.ndarray

    Return
    ------
    perm, make_swap : List[int], bool
    """
    swap_prob = 0.5
    make_swap = random.random() < swap_prob
    if n < 3:
        # In this case block-swaps don't make any sense
        make_swap = True
    if make_swap:
        # Choose what to swap
        i = random.randint(0, n - 1)
        j = i
        while j == i:
            j = random.randint(0, n - 1)
        # Define permutation
        perm = swap_1d(current_perm.copy(), i, j)
        # Define values after swap
        tmp_cm = swap(tmp_cm, i, j)
    else:
        # block-swap
        block_len = n
        while block_len >= n - 1:
            from_start = random.randint(0, n - 3)
            from_end = random.randint(from_start + 1, n - 2)
            block_len = from_start - from_end
        insert_pos = from_start
        while not (insert_pos < from_start or insert_pos > from_end):
            insert_pos = random.randint(0, n - 1)
        perm = move_1d(current_perm.copy(), from_start, from_end, insert_pos)

        # Define values after swap
        tmp_cm = move(tmp_cm, from_start, from_end, insert_pos)
    return perm, make_swap


def plot_cm(
    cm: np.ndarray,
    zero_diagonal: bool = False,
    labels: Optional[List[str]] = None,
    output: str = cfg["visualize"]["save_path"],
) -> None:
    """
    Plot a confusion matrix.

    Parameters
    ----------
    cm : np.ndarray
    zero_diagonal : bool, optional (default: False)
    labels : Optional[List[str]]
        If this is not given, then numbers are assigned to the classes
    """
    # Third party
    from matplotlib import pyplot as plt
    from matplotlib.colors import LogNorm

    n = len(cm)
    if zero_diagonal:
        for i in range(n):
            cm[i][i] = 0
    if n > 20:
        size = int(n / 4.0)
    else:
        size = 5
    fig = plt.figure(figsize=(size, size), dpi=80)
    plt.clf()
    ax = fig.add_subplot(111)
    ax.set_aspect(1)
    if labels is None:
        labels = [str(i) for i in range(len(cm))]
    x = list(range(len(cm)))
    plt.xticks(x, labels, rotation=cfg["visualize"]["xlabels_rotation"])
    y = list(range(len(cm)))
    plt.yticks(y, labels, rotation=cfg["visualize"]["ylabels_rotation"])
    if cfg["visualize"]["norm"] == "LogNorm":
        norm = LogNorm(vmin=max(1, np.min(cm)), vmax=np.max(cm))
    elif cfg["visualize"]["norm"] is None:
        norm = None
    else:
        raise NotImplementedError(
            "visualize->norm={} is not implemented. " "Try None or LogNorm"
        )
    res = ax.imshow(
        np.array(cm),
        cmap=cfg["visualize"]["colormap"],
        interpolation=cfg["visualize"]["interpolation"],
        norm=norm,
    )
    width, height = cm.shape

    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="5%", pad=0.5)
    plt.colorbar(res, cax=cax)
    plt.tight_layout()

    logger.info(f"Save figure at '{output}'")
    plt.savefig(output)


def create_html_cm(
    cm: np.ndarray, zero_diagonal: bool = False, labels: Optional[List[str]] = None
) -> None:
    """
    Plot a confusion matrix.

    Parameters
    ----------
    cm : np.ndarray
    zero_diagonal : bool, optional (default: False)
        If this is set to True, then the diagonal is overwritten with zeroes.
    labels : Optional[List[str]]
        If this is not given, then numbers are assigned to the classes
    """
    if labels is None:
        labels = [str(i) for i in range(len(cm))]

    el_max = 200

    template_path = resource_filename("clana", "templates/base.html")
    with open(template_path) as f:
        base = f.read()

    cm_t = cm.transpose()
    header_cells = []
    for i, label in enumerate(labels):
        precision = cm[i][i] / float(sum(cm_t[i]))
        background_color = "transparent"
        if precision < 0.2:
            background_color = "red"
        elif precision > 0.98:
            background_color = "green"
        header_cells.append(
            {
                "precision": f"{precision:0.2f}",
                "background-color": background_color,
                "label": label,
            }
        )

    body_rows = []
    for i, label, row in zip(range(len(labels)), labels, cm):
        body_row = []
        row_str = [str(el) for el in row]
        support = sum(row)
        recall = cm[i][i] / float(support)
        background_color = "transparent"
        if recall < 0.2:
            background_color = "red"
        elif recall >= 0.98:
            background_color = "green"
        body_row.append(
            {
                "label": label,
                "recall": f"{recall:.2f}",
                "background-color": background_color,
            }
        )
        for _j, pred_label, el in zip(range(len(labels)), labels, row_str):
            background_color = "transparent"
            if el == "0":
                el = ""
            else:
                background_color = get_color_code(float(el), el_max)

            body_row.append(
                {
                    "label": el,
                    "true": label,
                    "pred": pred_label,
                    "background-color": background_color,
                }
            )

        body_rows.append({"row": body_row, "support": support})

    html_template = Template(base)
    html = html_template.render(header_cells=header_cells, body_rows=body_rows)

    with open(cfg["visualize"]["html_save_path"], "w") as f:
        f.write(html)


def get_color(white_to_black: float) -> Tuple[int, int, int]:
    """
    Get grayscale color.

    Parameters
    ----------
    white_to_black : float

    Returns
    -------
    color : Tuple

    Examples
    --------
    >>> get_color(0)
    (255, 255, 255)
    >>> get_color(0.5)
    (128, 128, 128)
    >>> get_color(1)
    (0, 0, 0)
    """
    if not (0 <= white_to_black <= 1):
        raise ValueError("white_to_black={} is not in the interval [0, 1]")
    # in HSV, red is 0 deg and green is 120 deg (out of 360);
    # divide red_to_green with 3 to map [0, 1] to [0, 1./3.]
    # hue = red_to_green / 3.0
    # r, g, b = colorsys.hsv_to_rgb(hue, 0, 1)
    # return map(lambda x: int(255 * x), (r, g, b))

    index = 255 - int(255 * white_to_black)
    r, g, b = index, index, index
    return int(r), int(g), int(b)


def get_color_code(val: float, max_val: float) -> str:
    """
    Get a HTML color code which is between 0 and max_val.

    Parameters
    ----------
    val : number
    max_val : number

    Returns
    -------
    color_code : str

    Examples
    --------
    >>> get_color_code(0, 100)
    '#ffffff'
    >>> get_color_code(100, 100)
    '#000000'
    >>> get_color_code(50, 100)
    '#808080'
    """
    value = min(1.0, float(val) / max_val)
    r, g, b = get_color(value)
    return f"#{r:02x}{g:02x}{b:02x}"
