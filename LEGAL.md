# Legal Notes

## Hardware Interoperability

This project uses the timeBuzzer device via its standard USB-MIDI interface. No proprietary software is included, decompiled, or redistributed.

### Protocol Documentation

The MIDI protocol behavior documented in this project was obtained through standard protocol observation — monitoring MIDI messages sent and received by a lawfully purchased device using standard MIDI monitoring tools. MIDI is an open industry standard (maintained by the MIDI Manufacturers Association).

This approach is consistent with:

- **EU Trade Secrets Directive (2016/943)** and its German implementation in the **GeschGehG §3(1) Nr. 2**, which explicitly permits "observation, investigation, dismantling or testing of a product or object" that has been lawfully acquired.
- **EU Software Directive (2009/24/EC) Article 5(3)**, which permits a lawful user to "observe, study or test the functioning of the program in order to determine the ideas and principles which underlie any element of the program."

### What This Repository Contains

- Python scripts that communicate with the timeBuzzer over its USB-MIDI interface using standard MIDI CC messages
- Documentation of observed MIDI message behavior (CC numbers, value ranges)
- No decompiled code, no extracted source code, no proprietary assets from timeBuzzer GmbH

### What This Repository Does Not Contain

- No timeBuzzer application source code
- No extracted or decompiled binaries
- No proprietary protocols beyond standard MIDI observations
- No trademarks used in a way that implies endorsement

## Polar H10

Communication with the Polar H10 uses publicly documented Bluetooth Low Energy GATT services (Heart Rate Service 0x180D, Device Information Service 0x180A) and the Polar Measurement Data service as documented in the [Polar BLE SDK](https://github.com/polarofficial/polar-ble-sdk) (open source, BSD-3-Clause license).

## Philips Hue

Control of Philips Hue lights uses the official local HTTP API documented by Signify at [developers.meethue.com](https://developers.meethue.com/).

## Disclaimer

This project is for personal and educational use. It is not affiliated with, endorsed by, or sponsored by timeBuzzer GmbH, Polar Electro Oy, or Signify N.V. All product names are trademarks of their respective owners.

The health-related features (HRV analysis, stress detection, breathing guidance) are not medical devices and should not be used for medical diagnosis or treatment. Consult a healthcare professional for medical concerns.
