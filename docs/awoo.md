## Overview

FoxDash started without much of a goal.

I liked the idea of the driving apps that use OBD PIDs to show live engine data, but the more I looked at them, the more inefficient the whole approach seemed. One request, one reply, over and over again. Useful, but very limited.

So I started digging and found that I could bypass most of that and read data directly from the ECU over serial instead.

Still without any particular end goal, I then spent months logging drives and harvesting raw byte packets from the ECU. For a while, that mostly meant driving around with a laptop plugged into the car and collecting an unreasonable amount of data.

I worked through those logs looking for patterns, comparing them against my driving and deliberately creating controlled captures where I would only idle, change one condition, or perform a single action before ending the log. Some values could also be narrowed down using old blog posts from people working with similar ECUs. They were not identical, but close enough to provide useful clues.

I still have not decoded everything, and there is plenty of data I currently do nothing with, but eventually I had enough useful telemetry to start asking a different question: what did I actually want to see, what did I already have, and what could I calculate from it?

That is when the first real FoxDash interface appeared.

I used Textual partly as an excuse to learn it before bringing it into the Ember Deck, which turned out to be a very good decision. I also reused the BlinkStick lighting idea from Ember Deck so the system can give immediate feedback in the edge of my peripheral vision without requiring me to constantly read the display.

From there, most of the work became integration: getting everything to boot reliably on the Raspberry Pi, behave correctly in the car, and deal with things like startup voltage drops, ambient light levels, and automatic brightness.

There are still a few hardware issues to finish. I have a supercapacitor board on the way to help smooth out startup power, the ambient light sensor still needs tuning, and the final enclosure needs to be printed and fitted into the car.

Once everything is mounted, wired, and tuned properly, I'll consider that V1.

After that, I may build a mobile companion app.

Or I may not.

Depends how I feel.

## Key features

- Real-time mechanical strain estimation
- Separate driving economy scoring
- DPF and Eolys monitoring
- Advanced telemetry display
- Ambient-light and RGBW output support
- Physical display and vehicle integration
- Full long-term data logging
