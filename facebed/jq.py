



def nodes(obj: dict):
    result = []

    def collect(value):
        if isinstance(value, dict):
            result.append(value)
            for v in value.values():
                if isinstance(v, list):
                    collect(v)
            for v in value.values():
                if isinstance(v, dict):
                    collect(v)
            for v in value.values():
                if not isinstance(v, (dict, list)):
                    collect(v)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    collect(item)
            for item in value:
                if isinstance(item, list):
                    collect(item)
            for item in value:
                if not isinstance(item, (dict, list)):
                    collect(item)

    collect(obj)
    return result


def iterate(obj: dict, key: str, first: bool = False):
    result = []
    for oo in nodes(obj):
        if key in oo:
            if first:
                return oo[key]
            result.append(oo[key])
    return result


def find_all(obj: dict, key: str) -> list[dict]:
    return iterate(obj, key, first=False)


def find_first(obj: dict, key: str) -> dict:
    return iterate(obj, key, first=True)


def has(obj: dict, *args: str) -> bool:
    for k in args:
        found = False
        for oo in nodes(obj):
            if k in oo:
                found = True
                break
        if not found:
            return False
    return True
