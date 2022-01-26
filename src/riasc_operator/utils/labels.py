
def label_conflicts(a: dict[str, str], b: dict[str, str]) -> bool:
    """ Conflicts takes 2 maps and returns true if there a key match between
        the maps but the value doesn't match, and returns false in other cases """

    for k, v in a.items():
        if k in b and b[k] != v:
            return True

    return False
