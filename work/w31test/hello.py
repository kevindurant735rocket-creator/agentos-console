"""最小示例模块：hello.py"""


def greet(name):
    """返回 'hi <name>'。"""
    return f"hi {name}"


if __name__ == "__main__":
    print(greet("world"))
