package studio

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

type Segment struct {
	Start float64 `json:"start"`
	End   float64 `json:"end"`
}
type Word struct {
	Text  string  `json:"text"`
	Start float64 `json:"start"`
	End   float64 `json:"end"`
}
type Caption struct {
	Text      string  `json:"text"`
	Start     float64 `json:"start"`
	End       float64 `json:"end"`
	Highlight string  `json:"highlight,omitempty"`
	Words     []Word  `json:"words,omitempty"`
}
type Story struct {
	Hook            string `json:"hook"`
	Development     string `json:"development"`
	Payoff          string `json:"payoff"`
	Context         string `json:"context"`
	SelectionReason string `json:"selection_reason"`
}
type Music struct {
	Path       string `json:"path"`
	Creator    string `json:"creator"`
	Title      string `json:"title"`
	SourceURL  string `json:"source_url"`
	LicenseURL string `json:"license_url"`
}
type Effect struct {
	Kind   string    `json:"kind"`
	Start  float64   `json:"start"`
	End    float64   `json:"end"`
	Amount float64   `json:"amount,omitempty"`
	Box    []float64 `json:"box,omitempty"`
	Reason string    `json:"reason"`
}
type Plan struct {
	ID            string    `json:"id"`
	Source        string    `json:"source"`
	SceneMap      string    `json:"scene_map"`
	AudioTrack    int       `json:"audio_track"`
	Rating        string    `json:"rating"`
	Story         Story     `json:"story"`
	Segments      []Segment `json:"segments"`
	Captions      []Caption `json:"captions"`
	Music         *Music    `json:"music,omitempty"`
	Demonstration bool      `json:"demonstration,omitempty"`
	Hook          string    `json:"on_screen_hook,omitempty"`
	TextMotion    string    `json:"text_motion,omitempty"`
	Effects       []Effect  `json:"scene_effects,omitempty"`
}

func finite(n float64) bool { return !math.IsNaN(n) && !math.IsInf(n, 0) }
func plain(s string, limit int) bool {
	return strings.TrimSpace(s) != "" && len([]rune(s)) <= limit && !strings.ContainsAny(s, "{}\\\r\n")
}
func (p Plan) Duration() float64 {
	var n float64
	for _, s := range p.Segments {
		n += s.End - s.Start
	}
	return n
}
func (p Plan) Validate() error {
	if !regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`).MatchString(p.ID) {
		return errors.New("invalid story id")
	}
	if p.Source == "" || p.SceneMap == "" || p.AudioTrack < 0 {
		return errors.New("source, scene map and valid audio track required")
	}
	for _, s := range []string{p.Story.Hook, p.Story.Development, p.Story.Payoff, p.Story.Context, p.Story.SelectionReason} {
		if strings.TrimSpace(s) == "" {
			return errors.New("complete editorial story required")
		}
	}
	if p.Rating != "strong" && p.Rating != "medium" && p.Rating != "weak" {
		return errors.New("invalid rating")
	}
	if len(p.Segments) == 0 {
		return errors.New("no segments")
	}
	previous := 0.0
	for _, s := range p.Segments {
		if !finite(s.Start) || !finite(s.End) || s.Start < previous || s.End <= s.Start {
			return errors.New("segments must be chronological and non-overlapping")
		}
		previous = s.End
	}
	duration := p.Duration()
	previous = 0
	if p.TextMotion != "" && p.TextMotion != "none" && p.TextMotion != "pop" && p.TextMotion != "reveal" {
		return errors.New("unsupported text motion")
	}
	for _, c := range p.Captions {
		if !finite(c.Start) || !finite(c.End) || c.Start < previous || c.End <= c.Start || c.End > duration+.001 || !plain(c.Text, 100) {
			return errors.New("invalid caption")
		}
		previous = c.End
		if c.Highlight != "" && (!plain(c.Highlight, 100) || strings.Count(c.Text, c.Highlight) != 1) {
			return errors.New("highlight must occur once in caption")
		}
		if p.TextMotion == "reveal" && len(c.Words) == 0 {
			return errors.New("word reveal requires real word timestamps")
		}
		wordEnd := c.Start
		words := []string{}
		for _, w := range c.Words {
			if !plain(w.Text, 100) || !finite(w.Start) || !finite(w.End) || w.Start < wordEnd || w.End <= w.Start || w.End > c.End+.001 {
				return errors.New("invalid word timestamps")
			}
			wordEnd = w.End
			words = append(words, w.Text)
		}
		if len(words) > 0 && strings.Join(words, " ") != c.Text {
			return errors.New("words differ from caption")
		}
	}
	if p.Hook != "" && !plain(p.Hook, 55) {
		return errors.New("invalid hook")
	}
	if p.Music != nil {
		for _, s := range []string{p.Music.Path, p.Music.Creator, p.Music.Title, p.Music.SourceURL, p.Music.LicenseURL} {
			if strings.TrimSpace(s) == "" {
				return errors.New("music provenance required")
			}
		}
	}
	if len(p.Effects) > 8 {
		return errors.New("too many accents")
	}
	previous = 0
	for _, e := range p.Effects {
		if !finite(e.Start) || !finite(e.End) || e.Start < previous || e.End-e.Start < .3 || e.End > duration || strings.TrimSpace(e.Reason) == "" {
			return errors.New("invalid editorial accent")
		}
		previous = e.End
		switch e.Kind {
		case "punch_in":
			if !finite(e.Amount) || e.Amount < 0 || e.Amount > .08 {
				return errors.New("punch-in limited to 8 percent")
			}
		case "focus":
			if len(e.Box) != 4 || e.End-e.Start > 2.5 {
				return errors.New("invalid focus box/duration")
			}
			for _, n := range e.Box {
				if !finite(n) || n < 0 || n > 1 {
					return errors.New("focus coordinates outside image")
				}
			}
			if e.Box[2] <= 0 || e.Box[3] <= 0 || e.Box[0]+e.Box[2] > 1 || e.Box[1]+e.Box[3] > 1 {
				return errors.New("focus outside image")
			}
		default:
			return errors.New("unsupported accent")
		}
	}
	return nil
}

func HashFile(path string) (string, error) {
	f, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func Hash(v any) (string, error) {
	b, e := json.Marshal(v)
	if e != nil {
		return "", e
	}
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:]), nil
}
func ReadJSON(path string, v any) error {
	b, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	b = []byte(strings.TrimPrefix(string(b), "\ufeff"))
	d := json.NewDecoder(strings.NewReader(string(b)))
	if e = d.Decode(v); e != nil {
		return fmt.Errorf("%s: %w", path, e)
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return errors.New("trailing JSON data")
	}
	return nil
}
func WriteJSON(path string, v any) error {
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	return AtomicWrite(path, append(b, '\n'))
}
func AtomicWrite(path string, b []byte) error {
	if e := os.MkdirAll(filepath.Dir(path), 0755); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".write-*")
	if e != nil {
		return e
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if _, e = f.Write(b); e != nil {
		f.Close()
		return e
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	return os.Rename(tmp, path)
}
func ReadPlan(path string) (Plan, error) {
	var p Plan
	b, e := os.ReadFile(path)
	if e != nil {
		return p, e
	}
	decoder := json.NewDecoder(strings.NewReader(strings.TrimPrefix(string(b), "\ufeff")))
	decoder.DisallowUnknownFields()
	e = decoder.Decode(&p)
	if e != nil {
		return p, e
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return p, errors.New("trailing plan data")
	}
	return p, p.Validate()
}
