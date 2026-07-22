#!/usr/bin/env python3

import struct

from pymodbus.client import ModbusTcpClient

IP_ADDRESS = "192.168.1.2"
PORT = 502


def main() -> int:
    try:
        register_address = int(input("Enter register address: ").strip())
    except ValueError:
        print("Invalid register address")
        return 1

    client = ModbusTcpClient(IP_ADDRESS, port=PORT)

    if client.connect():
        result = client.read_holding_registers(
            address=register_address,
            count=4,
            device_id=1,
        )

        if not result or not hasattr(result, "registers"):
            print("Read failed")
            client.close()
            return 1

        regs = result.registers
        if len(regs) < 4:
            print("Unexpected register count:", len(regs))
            client.close()
            return 1

        raw = (
            regs[0].to_bytes(2, "big") +
            regs[1].to_bytes(2, "big") +
            regs[2].to_bytes(2, "big") +
            regs[3].to_bytes(2, "big")
        )

        value = struct.unpack(">Q", raw)[0]

        print("Value:", value)
        client.close()
        return 0

    print("Connection failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
