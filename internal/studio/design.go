package studio

import (
	"embed"
	"fmt"
	"math"
	"strings"
)

//go:embed fonts/*.ttf fonts/OFL.txt
var Fonts embed.FS

const DesignVersion = "go-motion-1"

func profileOK(profile string) bool {
	return profile == "accent" || profile == "clean" || profile == "cinema" || profile == "intrigue"
}
func assTime(t float64) string {
	n := int(math.Round(t * 100))
	return fmt.Sprintf("%d:%02d:%02d.%02d", n/360000, n/6000%60, n/100%60, n%100)
}
func captionText(c Caption, profile string, scale float64) string {
	text := c.Text
	if c.Highlight == "" {
		if profile == "accent" {
			return strings.ToUpper(text)
		}
		return text
	}
	before, after, _ := strings.Cut(text, c.Highlight)
	if profile == "accent" {
		prefix := strings.TrimSpace(strings.ToUpper(before))
		if prefix != "" {
			prefix += `\N`
		}
		return prefix + fmt.Sprintf(`{\c&H6BE8C6&\fs%d}%s{\rDefault}%s`, int(math.Round(96*scale)), strings.ToUpper(c.Highlight), strings.ToUpper(after))
	}
	return before + `{\c&H84DFF4&}` + c.Highlight + `{\rDefault}` + after
}
func Captions(p Plan, profile string, w, h int) (string, error) {
	if !profileOK(profile) {
		return "", fmt.Errorf("unknown profile %q", profile)
	}
	scale := float64(w) / 1080
	scaled := func(n float64) int { return int(math.Round(n * scale)) }
	size := 74.0
	italic := -1
	switch profile {
	case "clean":
		size = 70
		italic = 0
	case "cinema":
		size = 54
		italic = 0
	case "intrigue":
		size = 60
		italic = 0
	}
	var b strings.Builder
	fmt.Fprintf(&b, "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 0\nScaledBorderAndShadow: yes\n[V4+ Styles]\n", w, h)
	b.WriteString("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
	fmt.Fprintf(&b, "Style: Default,Montserrat,%d,&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,-1,%d,0,0,100,100,0,0,1,%.2f,0,8,%d,%d,%d,1\n", scaled(size), italic, 2.5*scale, scaled(90), scaled(180), int(float64(h)*.61))
	fmt.Fprintf(&b, "Style: Hook,Montserrat,%d,&H00151515,&H00151515,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,%d,0,8,%d,%d,%d,1\n", scaled(40), scaled(12), scaled(90), scaled(180), int(float64(h)*.12))
	fmt.Fprintf(&b, "Style: Demo,Montserrat,%d,&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,0,0,0,0,100,100,0,0,1,1,0,8,%d,%d,%d,1\n", scaled(25), scaled(90), scaled(180), int(float64(h)*.06))
	b.WriteString("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
	event := func(start, end float64, style, text string) {
		fmt.Fprintf(&b, "Dialogue: 1,%s,%s,%s,,0,0,0,,%s\n", assTime(start), assTime(end), style, text)
	}
	for _, c := range p.Captions {
		if p.TextMotion == "reveal" {
			for i, word := range c.Words {
				end := c.End
				if i+1 < len(c.Words) {
					end = c.Words[i+1].Start
				}
				var line strings.Builder
				for j, token := range c.Words {
					accent := false
					for _, key := range strings.Fields(c.Highlight) {
						if key == token.Text {
							accent = true
						}
					}
					separator := ""
					if j > 0 {
						separator = " "
					}
					if accent && j > 0 && !strings.Contains(" "+c.Highlight+" ", " "+c.Words[j-1].Text+" ") {
						separator = `\N`
					}
					line.WriteString(separator)
					color := `\c&HFFFFFF&`
					font := scaled(size)
					if accent {
						color = `\c&H6BE8C6&`
						font = scaled(96)
					}
					alpha := `\alpha&HFF&`
					if j <= i {
						alpha = `\alpha&H00&`
					}
					motion := `\fscx100\fscy100`
					if j == i {
						motion = `\fscx94\fscy94\t(0,110,\fscx100\fscy100)`
					}
					text := token.Text
					if profile == "accent" {
						text = strings.ToUpper(text)
					}
					fmt.Fprintf(&line, "{%s\\fs%d%s%s}%s", color, font, alpha, motion, text)
				}
				event(word.Start, end, "Default", line.String())
			}
		} else {
			entrance := `{\fad(55,55)}`
			if p.TextMotion == "pop" {
				entrance = `{\fad(45,55)\fscx92\fscy92\t(0,110,\fscx103\fscy103)\t(110,180,\fscx100\fscy100)}`
			}
			event(c.Start, c.End, "Default", entrance+captionText(c, profile, scale))
		}
	}
	if profile == "intrigue" && p.Hook != "" {
		event(0, math.Min(2.4, p.Duration()), "Hook", p.Hook)
	}
	if p.Demonstration {
		event(0, p.Duration(), "Demo", "ДЕМОНСТРАЦИЯ • ЛОКАЛЬНЫЙ ТЕСТ")
	}
	return b.String(), nil
}

func EffectFilters(effects []Effect, w, h int, fps float64) ([]string, string) {
	filters := []string{}
	current := "vjoin"
	for i, e := range effects {
		out := fmt.Sprintf("effect%d", i)
		if e.Kind == "punch_in" {
			amount := e.Amount
			if amount == 0 {
				amount = .055
			}
			first, last, ramp := e.Start*fps, e.End*fps, math.Max(2, .14*fps)
			z := fmt.Sprintf("1+%.5f*min(1,max(0,(on-%.4f)/%.4f))*min(1,max(0,(%.4f-on)/%.4f))", amount, first, ramp, last, ramp)
			filters = append(filters, fmt.Sprintf("[%s]zoompan=z='%s':x=iw/2-iw/zoom/2:y=ih/2-ih/zoom/2:d=1:s=%dx%d:fps=%.6f[%s]", current, z, w, h, fps, out))
		} else {
			x := min(w-2, int(math.Round(e.Box[0]*float64(w)/2))*2)
			y := min(h-2, int(math.Round(e.Box[1]*float64(h)/2))*2)
			bw := min(w-x, max(2, int(math.Round(e.Box[2]*float64(w)/2))*2))
			bh := min(h-y, max(2, int(math.Round(e.Box[3]*float64(h)/2))*2))
			filters = append(filters, fmt.Sprintf("[%s]split=3[orig%d][blur%d][detail%d]", current, i, i, i),
				fmt.Sprintf("[blur%d]gblur=sigma=7,eq=brightness=-0.10,format=yuva420p,fade=t=in:st=%.4f:d=0.14:alpha=1,fade=t=out:st=%.4f:d=0.14:alpha=1[wash%d]", i, e.Start, e.End-.14, i),
				fmt.Sprintf("[orig%d][wash%d]overlay=0:0:enable='between(t,%.4f,%.4f)'[dim%d]", i, i, e.Start, e.End, i),
				fmt.Sprintf("[detail%d]crop=%d:%d:%d:%d[roi%d]", i, bw, bh, x, y, i),
				fmt.Sprintf("[dim%d][roi%d]overlay=%d:%d:enable='between(t,%.4f,%.4f)'[%s]", i, i, x, y, e.Start, e.End, out))
		}
		current = out
	}
	return filters, current
}
