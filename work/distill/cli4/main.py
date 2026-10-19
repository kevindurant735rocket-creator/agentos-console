import sys
def main(argv=None):
    print(f"hello, {(argv or sys.argv[1:2] or ['world'])[0]}!")
if __name__ == "__main__":
    sys.exit(main())
