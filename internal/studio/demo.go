package studio

import (
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"html/template"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
)

var demoPhrases = []string{"Я не собираюсь платить.", "Тогда сделка отменяется.", "Вы уверены?"}

func synthVoices(m Media, folder string) error {
	missing := false
	for i := 0; i < 3; i++ {
		for _, suffix := range []string{".wav", ".wav.tsv"} {
			if _, e := os.Stat(filepath.Join(folder, fmt.Sprintf("voice-%d%s", i, suffix))); e != nil {
				missing = true
			}
		}
	}
	if !missing {
		return nil
	}
	if runtime.GOOS != "windows" {
		return errors.New("supply local voice-0.wav..voice-2.wav and .tsv word timestamps for demo")
	}
	root, e := filepath.Abs(folder)
	if e != nil {
		return e
	}
	if e = os.MkdirAll(root, 0755); e != nil {
		return e
	}
	ps := `$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$code=@'
using System; using System.Collections.Generic; using System.Globalization; using System.IO; using System.Text; using System.Speech.Synthesis;
public static class GoDemoVoice { public static void Save(string text,string path) {
 var words=new List<string>(); using(var synth=new SpeechSynthesizer()) {
 synth.SelectVoice("Microsoft Irina Desktop");synth.Rate=-1;
 synth.SpeakProgress+=(s,e)=>words.Add(e.AudioPosition.TotalSeconds.ToString("F4",CultureInfo.InvariantCulture)+"\t"+e.CharacterPosition+"\t"+e.CharacterCount+"\t"+e.Text);
 synth.SetOutputToWaveFile(path);synth.Speak(text); }
 File.WriteAllLines(path+".tsv",words,Encoding.UTF8); } }
'@
Add-Type -TypeDefinition $code -ReferencedAssemblies System.Speech
`
	for i, phrase := range demoPhrases {
		path := filepath.Join(root, fmt.Sprintf("voice-%d.wav", i))
		ps += fmt.Sprintf("[GoDemoVoice]::Save('%s','%s')\n", strings.ReplaceAll(phrase, "'", "''"), strings.ReplaceAll(path, "'", "''"))
	}
	units := utf16.Encode([]rune(ps))
	b := make([]byte, len(units)*2)
	for i, u := range units {
		binary.LittleEndian.PutUint16(b[2*i:], u)
	}
	cmd := exec.CommandContext(m.Context, "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", base64.StdEncoding.EncodeToString(b))
	out, e := cmd.CombinedOutput()
	if e != nil {
		return fmt.Errorf("local TTS failed: %w: %s", e, out)
	}
	return nil
}
func wavDuration(path string) (float64, error) {
	b, e := os.ReadFile(path)
	if e != nil {
		return 0, e
	}
	if len(b) < 12 || string(b[:4]) != "RIFF" {
		return 0, errors.New("not a WAV file")
	}
	rate, data := uint32(0), uint32(0)
	for at := 12; at+8 <= len(b); {
		size := int(binary.LittleEndian.Uint32(b[at+4:]))
		if at+8+size > len(b) {
			return 0, errors.New("incomplete WAV")
		}
		switch string(b[at : at+4]) {
		case "fmt ":
			if size >= 12 {
				rate = binary.LittleEndian.Uint32(b[at+16:])
			}
		case "data":
			data = uint32(size)
		}
		at += 8 + size + size%2
	}
	if rate == 0 || data == 0 {
		return 0, errors.New("missing PCM data")
	}
	return float64(data) / float64(rate), nil
}

type DemoVariant struct {
	Name        string  `json:"name"`
	Description string  `json:"description"`
	Path        string  `json:"video"`
	QA          Quality `json:"qa"`
}

func BuildMotionDemo(m Media, s *Store, image, voices, out string) error {
	var absErr error
	out, absErr = filepath.Abs(out)
	if absErr != nil {
		return absErr
	}
	if e := os.MkdirAll(out, 0755); e != nil {
		return e
	}
	if e := synthVoices(m, voices); e != nil {
		return e
	}
	image, e := filepath.Abs(image)
	if e != nil {
		return e
	}
	starts := []float64{.65}
	lengths := []float64{}
	voicePaths := []string{}
	voiceSHAs := []string{}
	for i := 0; i < 3; i++ {
		path, e := filepath.Abs(filepath.Join(voices, fmt.Sprintf("voice-%d.wav", i)))
		if e != nil {
			return e
		}
		duration, e := wavDuration(path)
		if e != nil {
			return e
		}
		lengths = append(lengths, duration)
		voicePaths = append(voicePaths, path)
		sha, e := HashFile(path)
		if e != nil {
			return e
		}
		voiceSHAs = append(voiceSHAs, sha)
		if i < 2 {
			starts = append(starts, starts[i]+duration+.55)
		}
	}
	duration := math.Round((starts[2]+lengths[2]+.8)*100) / 100
	imageSHA, e := HashFile(image)
	if e != nil {
		return e
	}
	key, _ := Hash(struct {
		Image  string
		Voices []string
	}{imageSHA, voiceSHAs})
	source, e := filepath.Abs(filepath.Join(out, "source-"+key[:12]+".mp4"))
	if e != nil {
		return e
	}
	if _, e = os.Stat(source); errors.Is(e, os.ErrNotExist) {
		args := []string{"-y", "-loop", "1", "-i", image}
		for _, path := range voicePaths {
			args = append(args, "-i", path)
		}
		filters := []string{"[0:v]scale=1080:1920,zoompan=z=1+on*0.00008:x=iw/2-iw/zoom/2:y=ih/2-ih/zoom/2:d=1:s=1080x1920:fps=25,format=yuv420p[v]"}
		for i, start := range starts {
			filters = append(filters, fmt.Sprintf("[%d:a]aresample=48000,adelay=%d:all=1[a%d]", i+1, int(start*1000+.5), i))
		}
		filters = append(filters, "[a0][a1][a2]amix=inputs=3:duration=longest:normalize=0,apad=pad_dur=1[a]")
		args = append(args, "-filter_complex", strings.Join(filters, ";"), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-t", f(duration), source)
		if _, e = m.Run("", time.Hour, args...); e != nil {
			return e
		}
	}
	music, e := filepath.Abs(filepath.Join(out, "pad.wav"))
	if e != nil {
		return e
	}
	if _, e = os.Stat(music); errors.Is(e, os.ErrNotExist) {
		_, e = m.Run("", time.Minute, "-y", "-f", "lavfi", "-i", fmt.Sprintf("aevalsrc=0.08*(sin(2*PI*110*t)+0.5*sin(2*PI*164.81*t)):s=48000:d=%s", f(duration)), music)
		if e != nil {
			return e
		}
	}
	sceneMap, e := Analyze(m, s, source)
	if e != nil {
		return e
	}
	p := Plan{ID: "motion-demo", Source: source, SceneMap: sceneMap, Rating: "medium", Demonstration: true, TextMotion: "reveal", Segments: []Segment{{0, duration}}, Story: Story{"Synthetic demo", "Synthetic demo", "Synthetic demo", "Not an acted movie scene", "Test motion locally"}, Music: &Music{music, "Local test generator", "Synthetic pad", "local:test", "local:demo-only"}}
	highlights := []string{"платить.", "отменяется.", "уверены?"}
	for i, text := range demoPhrases {
		c := Caption{Text: text, Start: starts[i], End: starts[i] + lengths[i], Highlight: highlights[i]}
		b, e := os.ReadFile(voicePaths[i] + ".tsv")
		if e != nil {
			return e
		}
		for _, line := range strings.Split(strings.TrimSpace(strings.TrimPrefix(string(b), "\ufeff")), "\n") {
			cols := strings.Split(strings.TrimSpace(line), "\t")
			if len(cols) < 4 {
				continue
			}
			t, e := strconv.ParseFloat(cols[0], 64)
			if e != nil {
				return e
			}
			position, e := strconv.Atoi(cols[1])
			if e != nil {
				return e
			}
			runes := []rune(text)
			if position < 0 || position >= len(runes) {
				return errors.New("invalid TTS word position")
			}
			token := strings.Fields(string(runes[position:]))[0]
			c.Words = append(c.Words, Word{token, starts[i] + t, c.End})
		}
		for j := 0; j+1 < len(c.Words); j++ {
			c.Words[j].End = c.Words[j+1].Start
		}
		p.Captions = append(p.Captions, c)
	}
	variants := []DemoVariant{}
	moment := p.Captions[1].Words[len(p.Captions[1].Words)-1].Start
	for i := 0; i < 3; i++ {
		candidate := p
		candidate.ID = fmt.Sprintf("motion-demo-%d", i+1)
		name, description := "01 — Текст по речи", "Слова появляются по моментам произнесения; ключевое слово выделяется."
		switch i {
		case 1:
			name = "02 — Поворот сцены"
			description = "Тот же текст плюс короткое приближение на ключевом ответе."
			candidate.Effects = []Effect{{Kind: "punch_in", Start: moment - .06, End: moment + .8, Amount: .06, Reason: "Accent on the decisive answer"}}
		case 2:
			name = "03 — Важная деталь"
			description = "Краткий фокус на конверте: окружение мягко размывается и затемняется."
			candidate.Effects = []Effect{{Kind: "focus", Start: 2.1, End: 3.4, Box: []float64{.10, .72, .67, .17}, Reason: "Demonstrate attention to the envelope"}}
		}
		if e = WriteJSON(filepath.Join(out, fmt.Sprintf("plan-%d.json", i+1)), candidate); e != nil {
			return e
		}
		file, e := Render(m, s, candidate, "accent", false)
		if e != nil {
			return e
		}
		q, e := Check(m, s, file)
		if e != nil {
			return e
		}
		if !q.Passed {
			return errors.New("demo QA failed")
		}
		relative, e := filepath.Rel(out, file)
		if e != nil {
			return e
		}
		variants = append(variants, DemoVariant{name, description, filepath.ToSlash(relative), q})
		fmt.Println("DEMO_CHECKED", i+1, file)
	}
	if e = WriteJSON(filepath.Join(out, "manifest.json"), variants); e != nil {
		return e
	}
	page := `<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Динамика ролика</title><style>*{box-sizing:border-box}body{margin:0;background:#111216;color:#fafafa;font:16px system-ui}main{max-width:1200px;margin:auto;padding:30px 20px}h1{font-size:28px}p{line-height:1.5;color:#c7c7cb}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}h2{font-size:18px}video{width:100%;aspect-ratio:9/16;border-radius:10px;background:#090909}button{font:inherit;border:0;border-radius:8px;padding:10px 18px;cursor:pointer;background:#c6e86b;color:#111;margin:12px 8px 20px 0}@media(max-width:700px){.grid{grid-template-columns:1fr}article{max-width:380px;margin:auto}}</style><main><h1>Движение и смысловые акценты</h1><p>Одинаковый текст и звук. Демонстрационный портрет с цифровым наездом, голос Windows TTS. Это не сыгранная сцена фильма. Эффекты расставлены вручную для сравнения.</p><button id="compare">Сравнить синхронно</button><button id="stop">Остановить</button><div class="grid">{{range .}}<article><h2>{{.Name}}</h2><p>{{.Description}}</p><video controls playsinline preload="metadata" src="{{.Path}}"></video></article>{{end}}</div></main><script>const videos=[...document.querySelectorAll('video')];document.getElementById('compare').onclick=()=>{videos.forEach((v,i)=>{v.currentTime=0;v.muted=i!==0;v.play().catch(()=>{})})};document.getElementById('stop').onclick=()=>videos.forEach(v=>v.pause());</script></html>`
	t, e := template.New("demo").Parse(page)
	if e != nil {
		return e
	}
	var b strings.Builder
	if e = t.Execute(&b, variants); e != nil {
		return e
	}
	return AtomicWrite(filepath.Join(out, "index.html"), []byte(b.String()))
}
